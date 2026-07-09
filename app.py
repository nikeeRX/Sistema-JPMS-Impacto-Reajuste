import os
import re
import traceback
import unicodedata
import pandas as pd
import numpy as np
import io
from datetime import datetime
from flask import Flask, request, render_template_string, redirect, url_for, flash, send_from_directory, send_file, make_response
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
from fpdf import FPDF

load_dotenv()

# =====================================================================
# CONSTANTES E MOTOR DE CRUZAMENTO
# =====================================================================
COL_EVENTO = 'EVENTO'
COL_VALOR_PAGO = 'VALOR_PAG'

def _remover_acentos(txt):
    if not txt: return ""
    return "".join(c for c in unicodedata.normalize("NFD", str(txt)) if unicodedata.category(c) != "Mn").upper()

def _limpar_texto_chave(txt):
    s = str(txt).strip().upper()
    return re.sub(r"\s+", " ", _remover_acentos(s))

def normalize_id_digits(val):
    if val is None: return ''
    s = str(val).strip()
    if s.endswith(".0"): s = s[:-2]
    return re.sub(r"\D", "", s)

def _find_column(df, candidates):
    if df is None or df.empty: return None
    cols_upper = {c.upper(): c for c in df.columns}
    for cand in candidates:
        if cand.upper() in cols_upper: return cols_upper[cand.upper()]
    return None

def cruzar_bases(df_fat, df_mat, df_die, df_dot, df_fai, df_pre):
    try:
        df = df_fat.copy()
        
        for col in ['UF', 'AP', 'CNPJ', 'NOME_FANTASIA_PRESTADOR']:
            if col not in df.columns: df[col] = None

        ev_col = _find_column(df, [COL_EVENTO, "EVENTO", "COD_EVENTO", "ESTRUTURA", "CODIGO"])
        ds_col = _find_column(df, ["DESCRICAO_EVENTO", "DESCRICAO", "EVENTO_DESC", "DESCRICAOEVENTO"])
        grau_col = _find_column(df, ["DESCRICAO_GRAU", "GRAU"])
        
        df["DESCRICAO_EVENTO"] = df[ds_col].fillna("").astype(str) if ds_col else ""
        df["_COD_LIMPO_"] = df[ev_col].fillna("").astype(str).apply(normalize_id_digits) if ev_col else ""
        
        df["ORIGEM_INICIAL"] = "Pendente"
        mask = (df["ORIGEM_INICIAL"] == "Pendente")
        
        if mask.any() and df_dot is not None and not df_dot.empty:
            df_d = df_dot.copy()
            d_ev = _find_column(df_d, ["ESTRUTURA", "CODIGO", "EVENTO"])
            d_ds = _find_column(df_d, ["DESC_EVENTO", "DESCRICAO"])
            
            df["CHAVE"] = df["_COD_LIMPO_"] + "-" + df["DESCRICAO_EVENTO"].apply(_limpar_texto_chave)
            df_d["CHAVE_DOT"] = df_d[d_ev].fillna("").astype(str).apply(normalize_id_digits) + "-" + df_d[d_ds].fillna("").astype(str).apply(_limpar_texto_chave)
            dot_keys = set(df_d["CHAVE_DOT"].unique())
            df.loc[mask, "ORIGEM_INICIAL"] = np.where(df.loc[mask, "CHAVE"].isin(dot_keys), "Dotação", "Faixa de Eventos")
        else: 
            df.loc[mask, "ORIGEM_INICIAL"] = "Faixa de Eventos"

        def get_ref_codes(rdf, is_dieta=False):
            if rdf is None or rdf.empty: return set()
            c = _find_column(rdf, ["ESTRUTURA", "CODIGO", "EVENTOS"]) if is_dieta else _find_column(rdf, ["EVENTOS", "EVENTO", "ESTRUTURA"])
            return set(rdf[c or rdf.columns[0]].fillna("").astype(str).apply(normalize_id_digits).unique())

        s_perf, s_diet = get_ref_codes(df_mat), get_ref_codes(df_die, True)
        
        t_col = _find_column(df, ["TIPO_DESPESA_FINAL", "TIPODESPESA", "TIPO", "TIPO_DESPESA", "GRUPO"])
        if t_col:
            df["TIPO_DESPESA_FINAL"] = df[t_col].fillna("OUTROS").astype(str).str.upper().str.strip()
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("MATERIA", na=False), "TIPO_DESPESA_FINAL"] = "MATERIAIS"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("MEDICA", na=False), "TIPO_DESPESA_FINAL"] = "MEDICAMENTOS"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("DIARIA", na=False), "TIPO_DESPESA_FINAL"] = "DIARIAS"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("TAXA", na=False), "TIPO_DESPESA_FINAL"] = "TAXAS"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("GASES", na=False), "TIPO_DESPESA_FINAL"] = "GASES"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("OPME|ORTESE|PROTESE", na=False), "TIPO_DESPESA_FINAL"] = "OPME"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("SADT|EXAME|DIAGNOSTICO|IMAGEM", na=False), "TIPO_DESPESA_FINAL"] = "SADT"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("HONORARIO|CONSULTA|VISITA|MEDICO", na=False), "TIPO_DESPESA_FINAL"] = "HONORARIOS"
        else:
            df["TIPO_DESPESA_FINAL"] = "OUTROS"
        
        if grau_col:
            mask_anestesista = df[grau_col].fillna("").astype(str).str.upper().isin(["ANESTESISTA", "AUXILIAR DE ANESTESISTA"])
            df.loc[mask_anestesista, "TIPO_DESPESA_FINAL"] = "ANESTESISTA"

        df.loc[df["_COD_LIMPO_"].isin(s_diet) & (df["_COD_LIMPO_"] != ""), "TIPO_DESPESA_FINAL"] = "DIETAS"
        df.loc[df["_COD_LIMPO_"].isin(s_perf) & (df["_COD_LIMPO_"] != "") & (df["TIPO_DESPESA_FINAL"] != "DIETAS"), "TIPO_DESPESA_FINAL"] = "PERFUROCORTANTES"
        
        return df.drop(columns=["CHAVE", "_COD_LIMPO_"], errors="ignore")
    except:
        traceback.print_exc()
        return pd.DataFrame()

# =====================================================================
# CLASSE DO GERADOR DE PDF
# =====================================================================
class ReajustePDF(FPDF):
    def header(self):
        if os.path.exists("Logo_Postal-03.png"):
            self.image("Logo_Postal-03.png", 10, 8, 35)
        self.set_text_color(18, 40, 63)
        self.set_font("Arial", "B", 16)
        self.cell(0, 10, "Relatorio de Impacto de Reajuste", 0, 1, "R")
        self.set_text_color(0, 0, 0)
        self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font("Arial", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Pagina {self.page_no()} | Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}", 0, 0, "C")

def build_analysis_pdf_bytes(data: dict) -> bytes:
    pdf = ReajustePDF()
    pdf.add_page()
    
    pdf.set_font("Arial", "B", 11)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(0, 8, "  DADOS DO PROCESSO", 0, 1, "L", fill=True)
    pdf.set_font("Arial", "", 10)
    pdf.cell(0, 7, f"Competencia Analisada: {data.get('comp', '-')}", 0, 1, "L")
    pdf.cell(0, 7, f"Modo de Aplicacao: {data.get('modo_aplicacao', 'POR TIPO')}", 0, 1, "L")
    pdf.cell(0, 7, f"IPCA do Periodo: {data.get('ipca', '0,00')}%", 0, 1, "L")
    pdf.cell(0, 7, f"Tipo de Negociacao: {data.get('tipo_neg', 'TODOS')}", 0, 1, "L")
    if data.get('tipo_neg') in ['ESTADO', 'MISTO']:
        pdf.cell(0, 7, f"UF Alvo: {data.get('uf_alvo', 'N/A')}", 0, 1, "L")
    if data.get('tipo_neg') in ['DIFERENCIADA', 'MISTO']:
        pdf.cell(0, 7, f"Alvo ({data.get('busca_por', 'CNPJ')}): {data.get('cnpj_alvo', 'N/A')}", 0, 1, "L")
    pdf.ln(5)

    categorias = {}
    for item in data.get("by_type", []):
        origem = item.get("origem", "Outros")
        if origem not in categorias: categorias[origem] = []
        categorias[origem].append(item)

    for cat, itens in categorias.items():
        pdf.set_font("Arial", "B", 11)
        pdf.set_text_color(18, 40, 63)
        pdf.cell(0, 10, f">> ORIGEM DA REGRA: {cat.upper()}", 0, 1, "L")
        
        pdf.set_text_color(255, 255, 255)
        pdf.set_fill_color(18, 40, 63)
        pdf.set_font("Arial", "B", 9)
        pdf.cell(75, 8, " Grupo de Despesa", 1, 0, "L", fill=True)
        pdf.cell(38, 8, "Base Lida", 1, 0, "C", fill=True)
        pdf.cell(38, 8, "Solicitado", 1, 0, "C", fill=True)
        pdf.cell(38, 8, "Concedido", 1, 1, "C", fill=True)
        
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Arial", "", 8)
        for i, item in enumerate(itens):
            fill = i % 2 == 0
            if fill: pdf.set_fill_color(250, 250, 250)
            else: pdf.set_fill_color(255, 255, 255)
            
            tipo_txt = str(item["tipo"])[:50]
            pdf.cell(75, 7, f" {tipo_txt}", 1, 0, "L", fill=True)
            pdf.cell(38, 7, f"R$ {item['valor']:,.2f} ", 1, 0, "R", fill=True)
            pdf.cell(38, 7, f"R$ {item['delta_solicitado']:,.2f} ", 1, 0, "R", fill=True)
            pdf.cell(38, 7, f"R$ {item['delta_concedido']:,.2f} ", 1, 1, "R", fill=True)
        pdf.ln(5)

    if pdf.get_y() > 200: pdf.add_page()
    pdf.set_font("Arial", "B", 12)
    pdf.set_text_color(18, 40, 63)
    pdf.cell(0, 10, "RESUMO FINANCEIRO", "B", 1, "L")
    pdf.ln(2)
    
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", "", 10)
    totals = data.get("totals", {})
    fat_total = totals.get('total_faturamento', 0)
    sol_total = totals.get('total_solicited', 0)
    conc_total = totals.get('total_conceded', 0)
    custo_evitado = totals.get('custo_evitado', 0)

    pdf.cell(100, 8, "Faturamento Total Lido (Base):", 0, 0)
    pdf.cell(0, 8, f"R$ {fat_total:,.2f}", 0, 1, "R")
    
    pdf.cell(100, 8, "Total do Reajuste Solicitado:", 0, 0)
    pdf.cell(0, 8, f"R$ {sol_total:,.2f}", 0, 1, "R")
    
    pdf.cell(100, 8, "Total do Reajuste Concedido:", 0, 0)
    pdf.cell(0, 8, f"R$ {conc_total:,.2f}", 0, 1, "R")
    
    pdf.set_font("Arial", "B", 11)
    if custo_evitado > 0: pdf.set_text_color(0, 100, 0)
    else: pdf.set_text_color(200, 0, 0)

    pdf.cell(100, 10, "CUSTO EVITADO (ECONOMIA):", 0, 0)
    pdf.cell(0, 10, f"R$ {custo_evitado:,.2f}", 0, 1, "R")
    pdf.ln(15)

    if pdf.get_y() > 250: pdf.add_page()
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", "", 10)
    y_sig = pdf.get_y()
    pdf.line(20, y_sig+15, 90, y_sig+15)
    pdf.line(120, y_sig+15, 190, y_sig+15)
    pdf.set_y(y_sig + 17)
    pdf.cell(95, 5, data.get("analista", "Analista Responsavel"), 0, 0, "C")
    pdf.cell(95, 5, data.get("gestor", "Gestor Aprovador"), 0, 1, "C")
    
    return pdf.output(dest="S").encode("latin-1", errors="ignore")

# =====================================================================
# BANCO DE DADOS E CONFIGURAÇÃO FLASK
# =====================================================================
app = Flask(__name__)
app.secret_key = "chave_secreta_super_segura_gered"

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(DATABASE_URL) if DATABASE_URL else None

def obter_linhas_tabela():
    resumos = {}
    mapeamento = {
        'faturamento': 'Faturamento Mensal', 'prestadores': 'Cadastro de Prestadores',
        'materiais': 'Materiais Perfurocortantes', 'dietas': 'Tabela de Dietas',
        'dotacoes': 'Base de Dotações', 'faixas': 'Faixa de Eventos'
    }
    for t_nome, t_desc in mapeamento.items():
        if not engine:
            resumos[t_nome] = {'desc': t_desc, 'status': 'Sem Conexão', 'linhas': 0}
            continue
        try:
            with engine.connect() as conn:
                qtd = conn.execute(text(f"SELECT COUNT(*) FROM {t_nome}")).scalar()
                resumos[t_nome] = {'desc': t_desc, 'status': 'Ativa', 'linhas': qtd}
        except:
            resumos[t_nome] = {'desc': t_desc, 'status': 'Vazia', 'linhas': 0}
    return resumos

# =====================================================================
# HTML EMBUTIDOS (DESIGN IDENTIDADE VISUAL MODERNO E TABS)
# =====================================================================
CSS_PADRAO = """
<style>
    :root { --azul-postal: #002c52; --azul-claro: #005a92; --amarelo-postal: #f9b200; --verde-ok: #007a33; --vermelho-alerta: #cc0000; --fundo: #f4f7f6; }
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--fundo); color: #333; margin: 0; display: flex; min-height: 100vh; }
    
    .sidebar { width: 280px; background-color: white; border-right: 1px solid #ddd; padding: 20px; display: flex; flex-direction: column; gap: 20px; box-shadow: 2px 0 5px rgba(0,0,0,0.05); z-index: 10; }
    .main-content { flex: 1; display: flex; flex-direction: column; overflow-y: auto; }
    
    .logo-img { max-width: 220px; height: auto; object-fit: contain; margin-bottom: 10px; }
    .sidebar-section { border-bottom: 1px solid #eee; padding-bottom: 15px; }
    .sidebar-section h4 { color: var(--azul-postal); margin: 0 0 10px 0; font-size: 1em; }
    
    .header { background-color: white; padding: 20px 40px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #ddd; }
    .header h2 { margin: 0; color: var(--azul-postal); font-size: 1.5em; }
    
    .container { padding: 30px 40px; }
    .card { background: white; padding: 25px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.05); margin-bottom: 20px; border-top: 4px solid var(--azul-claro); }
    
    .form-group { margin-bottom: 15px; }
    .form-group label { display: block; margin-bottom: 5px; font-weight: bold; color: #555; font-size: 0.85em; }
    .form-control { width: 100%; padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; font-size: 0.95em; transition: border-color 0.3s; }
    .form-control:focus { border-color: var(--azul-claro); outline: none; }
    
    .radio-group { display: flex; gap: 15px; align-items: center; margin-top: 5px; }
    .radio-group label { font-weight: normal; margin: 0; cursor: pointer; display: flex; align-items: center; gap: 5px; }
    
    .dynamic-block { display: none; background: #fafafa; padding: 15px; border: 1px dashed #ccc; border-radius: 5px; margin-top: 15px; animation: fadeIn 0.3s; }
    @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

    /* ESTILO DAS ABAS (TABS) */
    .tabs { display: flex; border-bottom: 2px solid #ddd; margin-bottom: 20px; gap: 5px; }
    .tab-link { padding: 12px 25px; cursor: pointer; font-weight: bold; color: #666; border-bottom: 3px solid transparent; transition: 0.3s; font-size: 0.95em; background: none; border-top: none; border-left: none; border-right: none; }
    .tab-link:hover { color: var(--azul-claro); }
    .tab-link.active { color: var(--azul-postal); border-bottom-color: var(--amarelo-postal); }
    .tab-content { display: none; animation: fadeIn 0.3s; }
    .tab-content.active { display: block; }

    /* ESTILO DA LISTA DE DESPESAS */
    .expense-row { display: flex; justify-content: space-between; align-items: center; padding: 12px; border-bottom: 1px solid #eee; transition: background 0.2s; }
    .expense-row:hover { background-color: #f9f9f9; }
    .expense-label { font-weight: bold; color: #333; font-size: 0.9em; flex: 1; }
    .expense-inputs { display: flex; gap: 15px; align-items: center; }
    .input-wrapper { display: flex; flex-direction: column; align-items: flex-start; }
    .input-wrapper label { font-size: 0.75em; color: #888; font-weight: normal; margin-bottom: 2px; }
    .input-wrapper input { width: 100px; padding: 6px; border: 1px solid #ccc; border-radius: 4px; text-align: right; }

    .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; }
    .metric-box { padding: 15px; border: 1px solid #eee; border-radius: 6px; text-align: center; background: #fafafa; }
    .metric-box h4 { margin: 0 0 10px 0; color: #666; font-size: 0.85em; text-transform: uppercase; }
    .metric-box .valor { font-size: 1.6em; font-weight: bold; color: var(--azul-postal); margin-bottom: 5px; }
    .metric-box .sub { font-size: 0.85em; color: #888; }
    
    table { width: 100%; border-collapse: collapse; margin-top: 15px; }
    th { background-color: var(--azul-postal); color: white; padding: 12px; text-align: left; font-size: 0.9em; }
    td { padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 0.9em; }
    tr:hover { background-color: #f4f7f6; }
    
    .btn { background-color: var(--azul-claro); color: white; font-weight: bold; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; text-align: center; transition: background 0.3s; }
    .btn:hover { background-color: var(--azul-postal); }
    .btn-action { background-color: var(--amarelo-postal); color: var(--azul-postal); width: 100%; font-size: 1.1em; padding: 12px; margin-top: 10px; }
    .btn-action:hover { background-color: #e0a100; }
    .btn-success { background-color: var(--verde-ok); }
    .btn-pdf { background-color: #cc0000; margin-left: 10px; }
    .btn-danger { background-color: var(--vermelho-alerta); padding: 5px 10px; font-size: 0.85em; }
    .alert { padding: 15px; border-radius: 4px; margin-bottom: 20px; }
    .alert-danger { background: #fde8e8; color: var(--vermelho-alerta); border: 1px solid var(--vermelho-alerta); }
</style>

<script>
    function openTab(evt, tabName) {
        var i, tabcontent, tablinks;
        tabcontent = document.getElementsByClassName("tab-content");
        for (i = 0; i < tabcontent.length; i++) {
            tabcontent[i].style.display = "none";
            tabcontent[i].className = tabcontent[i].className.replace(" active", "");
        }
        tablinks = document.getElementsByClassName("tab-link");
        for (i = 0; i < tablinks.length; i++) {
            tablinks[i].className = tablinks[i].className.replace(" active", "");
        }
        document.getElementById(tabName).style.display = "block";
        document.getElementById(tabName).className += " active";
        evt.currentTarget.className += " active";
    }

    function toggleFiltros() {
        var neg = document.getElementById("tipo_neg").value;
        var divEst = document.getElementById("div_estado");
        var divDif = document.getElementById("div_diferenciada");
        divEst.style.display = (neg === "ESTADO" || neg === "MISTO") ? "block" : "none";
        divDif.style.display = (neg === "DIFERENCIADA" || neg === "MISTO") ? "block" : "none";
    }

    function toggleModo() {
        var radios = document.getElementsByName('modo_aplicacao');
        var modo = 'POR_TIPO';
        for (var i = 0; i < radios.length; i++) {
            if (radios[i].checked) { modo = radios[i].value; break; }
        }
        var divTipo = document.getElementById('div_por_tipo');
        var divLinear = document.getElementById('div_linear');
        if (modo === 'LINEAR') {
            divTipo.style.display = 'none';
            divLinear.style.display = 'block';
        } else {
            divTipo.style.display = 'block';
            divLinear.style.display = 'none';
        }
    }

    window.onload = function() {
        toggleFiltros();
        toggleModo();
    };
</script>
"""

HTML_DASHBOARD = CSS_PADRAO + """
<!-- SIDEBAR (MENU LATERAL) -->
<form action="/" method="get" id="mainForm" style="display: contents;">
    <div class="sidebar">
        <img src="/Logo_Postal-03.png" class="logo-img" alt="Postal Saúde">
        
        <div class="sidebar-section">
            <h4>Configurações para reajustar</h4>
            <div class="form-group">
                <label>Modo de aplicação</label>
                <div class="radio-group">
                    <label><input type="radio" name="modo_aplicacao" value="POR_TIPO" onclick="toggleModo()" {% if filtros.modo_aplicacao != 'LINEAR' %}checked{% endif %}> Por tipo</label>
                    <label><input type="radio" name="modo_aplicacao" value="LINEAR" onclick="toggleModo()" {% if filtros.modo_aplicacao == 'LINEAR' %}checked{% endif %}> Linear</label>
                </div>
            </div>
            <div class="form-group" style="margin-top: 15px;">
                <label>IPCA do período (%)</label>
                <input type="number" step="0.01" name="ipca" class="form-control" value="{{ filtros.ipca }}">
            </div>
            <div class="form-group">
                <label>Período da Análise (Competência)</label>
                <input type="text" name="comp" class="form-control" value="{{ filtros.comp }}" placeholder="Ex: 092024" required>
            </div>
        </div>

        <div class="sidebar-section" style="border-bottom: none;">
            <h4>Identificação e Assinaturas</h4>
            <div class="form-group">
                <label>Nome do Analista</label>
                <input type="text" name="analista" class="form-control" value="{{ filtros.analista }}">
            </div>
            <div class="form-group">
                <label>Nome do Gestor</label>
                <input type="text" name="gestor" class="form-control" value="{{ filtros.gestor }}">
            </div>
        </div>
        
        <div style="flex-grow: 1;"></div>
        <button type="submit" class="btn btn-action">Carregar e Cruzar Bases</button>
    </div>

    <!-- MAIN CONTENT (ÁREA PRINCIPAL) -->
    <div class="main-content">
        <div class="header">
            <h2>Sistema de reajuste de discussão</h2>
            <a href="/admin" class="btn" style="background:#eef2f5; color:var(--azul-postal); border:1px solid #ccc;">Painel Admin BD</a>
        </div>

        <div class="container">
            {% with messages = get_flashed_messages(category_filter=["error"]) %}{% if messages %}{% for m in messages %}<div class="alert alert-danger">{{ m }}</div>{% endfor %}{% endif %}{% endwith %}

            <!-- FILTRO DE NEGOCIAÇÃO -->
            <div class="card" style="border-top: none;">
                <div class="form-group" style="max-width: 400px;">
                    <label style="font-size: 1em; color: var(--azul-postal);">Filtrar por NEGOCIAÇÃO</label>
                    <select name="tipo_neg" id="tipo_neg" class="form-control" onchange="toggleFiltros()" style="font-size: 1.1em; padding: 10px;">
                        <option value="TODOS" {% if filtros.tipo_neg == 'TODOS' %}selected{% endif %}>Selecione as opções (Padrão: Todos)</option>
                        <option value="DIFERENCIADA" {% if filtros.tipo_neg == 'DIFERENCIADA' %}selected{% endif %}>DIFERENCIADA</option>
                        <option value="ESTADO" {% if filtros.tipo_neg == 'ESTADO' %}selected{% endif %}>ESTADO</option>
                        <option value="MISTO" {% if filtros.tipo_neg == 'MISTO' %}selected{% endif %}>MISTO</option>
                    </select>
                </div>

                <div id="div_estado" class="dynamic-block">
                    <div class="form-group" style="max-width: 200px;">
                        <label>Digite a UF Alvo (Ex: GO, RS):</label>
                        <input type="text" name="uf_alvo" class="form-control" value="{{ filtros.uf_alvo }}">
                    </div>
                </div>

                <div id="div_diferenciada" class="dynamic-block">
                    <div class="form-group">
                        <label>Buscar por:</label>
                        <div class="radio-group">
                            <label><input type="radio" name="busca_por" value="CNPJ" {% if filtros.busca_por != 'GRUPO' %}checked{% endif %}> CNPJ</label>
                            <label><input type="radio" name="busca_por" value="GRUPO" {% if filtros.busca_por == 'GRUPO' %}checked{% endif %}> GRUPOPRESTADOR / NOME</label>
                        </div>
                    </div>
                    <div class="form-group" style="max-width: 400px;">
                        <label>Digite o CNPJ ou Nome:</label>
                        <input type="text" name="cnpj_alvo" class="form-control" value="{{ filtros.cnpj_alvo }}" placeholder="Digite o texto de busca...">
                    </div>
                </div>
            </div>

            <!-- BLOCO DE TAXAS (COM ABAS) -->
            <div class="card" id="div_por_tipo">
                <div class="tabs">
                    <button type="button" class="tab-link active" onclick="openTab(event, 'tab-dotacao')">Dotação (Geral)</button>
                    <button type="button" class="tab-link" onclick="openTab(event, 'tab-faixa')">Faixa de Evento (Geral)</button>
                    <button type="button" class="tab-link" onclick="openTab(event, 'tab-especificos')">Itens Específicos (Exceções)</button>
                </div>

                <!-- ABA 1: DOTAÇÃO -->
                <div id="tab-dotacao" class="tab-content active">
                    <p style="color:#666; font-size:0.9em;">Regra base aplicada a todos os itens classificados como Dotação (que não forem extraídos para Itens Específicos).</p>
                    <div style="display:flex; gap:20px; align-items:center; background:#f4f7f6; padding:20px; border-radius:6px; max-width:400px;">
                        <div class="input-wrapper">
                            <label>% Solicitado</label>
                            <input type="number" step="0.01" name="sol_dotacao" value="{{ filtros.sol_dotacao }}">
                        </div>
                        <div class="input-wrapper">
                            <label>% Concedido</label>
                            <input type="number" step="0.01" name="conc_dotacao" value="{{ filtros.conc_dotacao }}">
                        </div>
                    </div>
                </div>

                <!-- ABA 2: FAIXA DE EVENTO -->
                <div id="tab-faixa" class="tab-content">
                    <p style="color:#666; font-size:0.9em;">Regra base aplicada a todos os itens classificados como Faixa de Evento (que não forem extraídos para Itens Específicos).</p>
                    <div style="display:flex; gap:20px; align-items:center; background:#f4f7f6; padding:20px; border-radius:6px; max-width:400px;">
                        <div class="input-wrapper">
                            <label>% Solicitado</label>
                            <input type="number" step="0.01" name="sol_faixa" value="{{ filtros.sol_faixa }}">
                        </div>
                        <div class="input-wrapper">
                            <label>% Concedido</label>
                            <input type="number" step="0.01" name="conc_faixa" value="{{ filtros.conc_faixa }}">
                        </div>
                    </div>
                </div>

                <!-- ABA 3: ITENS ESPECÍFICOS -->
                <div id="tab-especificos" class="tab-content">
                    <p style="color:#cc0000; font-size:0.9em; font-weight:bold;">Atenção: Ao preencher qualquer taxa abaixo (diferente de 0), o item é RETIRADO da regra geral (Dotação/Faixa) e calculado separadamente.</p>
                    
                    {% set esp = [
                        ('DIETAS', 'dietas'), ('PERFUROCORTANTES', 'perfuro'), ('ANESTESISTA', 'anest'),
                        ('MATERIAIS', 'mat'), ('MEDICAMENTOS', 'med'), ('DIÁRIAS', 'dia'),
                        ('TAXAS', 'taxa'), ('GASES MEDICINAIS', 'gas'), ('OPME', 'opme'),
                        ('SADT / EXAMES', 'sadt'), ('HONORÁRIOS', 'hon'), ('OUTROS / GERAL', 'outros')
                    ] %}
                    
                    {% for label, key in esp %}
                    <div class="expense-row">
                        <div class="expense-label">{{ label }}</div>
                        <div class="expense-inputs">
                            <div class="input-wrapper">
                                <label>% Sol.</label>
                                <input type="number" step="0.01" name="sol_{{ key }}" value="{{ filtros['sol_'~key] }}">
                            </div>
                            <div class="input-wrapper">
                                <label>% Conc.</label>
                                <input type="number" step="0.01" name="conc_{{ key }}" value="{{ filtros['conc_'~key] }}">
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            
            <!-- BLOCO DINÂMICO: LINEAR -->
            <div id="div_linear" style="display:none; background:#fde8e8; padding:20px; border-radius:8px; border:1px solid var(--vermelho-alerta); margin-bottom:20px;">
                <h3 style="margin: 0 0 5px 0; color: var(--vermelho-alerta);">Modo Linear Ativado</h3>
                <p style="font-size:0.9em; color:#666; margin-bottom:15px;">A mesma taxa será aplicada a todas as linhas do faturamento, esmagando regras de Dotação, Faixa e Exceções.</p>
                <div style="display:flex; gap:20px;">
                    <div class="form-group" style="max-width: 150px;">
                        <label>Linear Solicitado (%):</label>
                        <input type="number" step="0.01" name="sol_linear" class="form-control" value="{{ filtros.sol_linear }}">
                    </div>
                    <div class="form-group" style="max-width: 150px;">
                        <label>Linear Concedido (%):</label>
                        <input type="number" step="0.01" name="conc_linear" class="form-control" value="{{ filtros.conc_linear }}">
                    </div>
                </div>
            </div>

            <!-- RESULTADOS -->
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <h3 style="margin:0; color: var(--azul-postal);">Resumo Financeiro Consolidado</h3>
                    {% if tem_dados %}
                    <div>
                        {% set export_params = 'comp='~filtros.comp~'&tipo_neg='~filtros.tipo_neg~'&uf_alvo='~filtros.uf_alvo~'&cnpj_alvo='~filtros.cnpj_alvo~'&busca_por='~filtros.busca_por~'&modo_aplicacao='~filtros.modo_aplicacao~'&ipca='~filtros.ipca~'&analista='~filtros.analista~'&gestor='~filtros.gestor~'&sol_dotacao='~filtros.sol_dotacao~'&conc_dotacao='~filtros.conc_dotacao~'&sol_faixa='~filtros.sol_faixa~'&conc_faixa='~filtros.conc_faixa~'&sol_linear='~filtros.sol_linear~'&conc_linear='~filtros.conc_linear~'&sol_dietas='~filtros.sol_dietas~'&conc_dietas='~filtros.conc_dietas~'&sol_perfuro='~filtros.sol_perfuro~'&conc_perfuro='~filtros.conc_perfuro~'&sol_anest='~filtros.sol_anest~'&conc_anest='~filtros.conc_anest~'&sol_mat='~filtros.sol_mat~'&conc_mat='~filtros.conc_mat~'&sol_med='~filtros.sol_med~'&conc_med='~filtros.conc_med~'&sol_dia='~filtros.sol_dia~'&conc_dia='~filtros.conc_dia~'&sol_taxa='~filtros.sol_taxa~'&conc_taxa='~filtros.conc_taxa~'&sol_gas='~filtros.sol_gas~'&conc_gas='~filtros.conc_gas~'&sol_opme='~filtros.sol_opme~'&conc_opme='~filtros.conc_opme~'&sol_sadt='~filtros.sol_sadt~'&conc_sadt='~filtros.conc_sadt~'&sol_hon='~filtros.sol_hon~'&conc_hon='~filtros.conc_hon~'&sol_outros='~filtros.sol_outros~'&conc_outros='~filtros.conc_outros %}
                        <a href="/exportar?{{ export_params }}" class="btn btn-success">📥 Excel</a>
                        <a href="/exportar_pdf?{{ export_params }}" class="btn btn-pdf" target="_blank">📄 PDF</a>
                    </div>
                    {% endif %}
                </div>
                
                <div class="grid-4" style="margin-top: 15px;">
                    <div class="metric-box">
                        <h4>Faturamento Lido</h4>
                        <div class="valor">R$ {{ totais.faturamento_total }}</div>
                        <div class="sub">{{ totais.linhas_faturamento }} linhas</div>
                    </div>
                    <div class="metric-box">
                        <h4>Total Solicitado</h4>
                        <div class="valor" style="color: var(--azul-claro);">R$ {{ totais.total_solicitado }}</div>
                        <div class="sub">Pós-regras (Sol.)</div>
                    </div>
                    <div class="metric-box">
                        <h4>Total Concedido</h4>
                        <div class="valor" style="color: var(--verde-ok);">R$ {{ totais.total_concedido }}</div>
                        <div class="sub">Pós-regras (Conc.)</div>
                    </div>
                    <div class="metric-box impacto-card">
                        <h4>Custo Evitado</h4>
                        <div class="valor" style="color: var(--vermelho-alerta);">R$ {{ totais.custo_evitado }}</div>
                        <div class="sub">Solicitado - Concedido</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <h3 style="margin-top:0; color: var(--azul-postal);">Detalhamento de Impacto (Separação Específica)</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Grupo de Despesa</th>
                            <th>Origem do Cálculo</th>
                            <th>Itens</th>
                            <th>Base Lida (R$)</th>
                            <th>Solicitado (R$)</th>
                            <th>Concedido (R$)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for item in itens_detalhe %}
                        <tr>
                            <td><strong>{{ item.tipo_despesa }}</strong></td>
                            <td>
                                {% if item.origem == 'Item Específico' %}
                                    <span style="background: #fff3cd; color:#856404; padding: 4px 8px; border-radius: 4px; font-weight:bold; font-size:0.85em;">{{ item.origem }}</span>
                                {% elif item.origem == 'Modo Linear' %}
                                    <span style="background: #f8d7da; color:#721c24; padding: 4px 8px; border-radius: 4px; font-weight:bold; font-size:0.85em;">{{ item.origem }}</span>
                                {% else %}
                                    <span style="background: #eef2f5; padding: 4px 8px; border-radius: 4px; font-size:0.85em;">{{ item.origem }}</span>
                                {% endif %}
                            </td>
                            <td>{{ item.qtd }}</td>
                            <td>R$ {{ item.valor_base }}</td>
                            <td>R$ {{ item.valor_sol }}</td>
                            <td style="color: var(--verde-ok); font-weight: bold;">R$ {{ item.valor_con }}</td>
                        </tr>
                        {% else %}
                        <tr>
                            <td colspan="6" style="text-align: center; color: #888; padding: 30px;">Aguardando processamento.</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</form>
"""

HTML_ADMIN = CSS_PADRAO + """
<div class="header">
    <div style="display: flex; align-items: center; gap: 20px;">
        <img src="/Logo_Postal-03.png" class="logo-img" alt="Postal Saúde">
        <h2 style="margin:0;">Administração de Banco de Dados</h2>
    </div>
    <a href="/" class="btn">Voltar ao Dashboard</a>
</div>

<div class="container">
    {% with messages = get_flashed_messages(category_filter=["success"]) %}{% if messages %}{% for message in messages %}<div class="alert alert-success">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}
    {% with messages = get_flashed_messages(category_filter=["error"]) %}{% if messages %}{% for message in messages %}<div class="alert alert-danger">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}

    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
        <div class="card" style="border-top: 4px solid var(--azul-claro);">
            <h3 style="margin-top:0; color: var(--azul-postal);">Upload Inteligente em Massa</h3>
            
            <form id="upload-form" action="/admin_upload" method="post" enctype="multipart/form-data">
                <div class="form-group">
                    <label>Selecione a Base de Destino:</label>
                    <select name="tipo_base" style="width: 100%; padding: 10px;" required>
                        <option value="faturamento">Faturamento Mensal</option>
                        <option value="dotacoes">Base de Dotações</option>
                        <option value="materiais">Materiais Perfurocortantes</option>
                        <option value="dietas">Dietas</option>
                        <option value="faixas">Faixa de Eventos</option>
                        <option value="prestadores">Prestadores</option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>Competência Fixa (Opcional):</label>
                    <input type="text" name="competencia" style="width: 100%; padding: 10px;" placeholder="Deixe VAZIO para usar o nome do arquivo">
                    <small style="color: #666;">Se não preencher, o sistema usará o nome do arquivo</small>
                </div>

                <div class="form-group">
                    <label>Selecione a PASTA INTEIRA com os arquivos (Upload total):</label>
                    <input type="file" name="arquivos_pasta" style="width: 100%; padding: 15px; background: #fafafa; border: 2px dashed #005a92;" webkitdirectory directory multiple>
                </div>
                
                <div class="form-group">
                    <label>OU Selecione arquivos soltos manualmente (Segure CTRL):</label>
                    <input type="file" name="arquivos_soltos" style="width: 100%; padding: 15px; background: #fafafa; border: 2px dashed #999;" multiple>
                </div>
                
                <button type="submit" id="upload-btn" class="btn btn-success" style="width: 100%; font-size: 16px; padding: 12px;">Injetar Dados no Servidor</button>
                
                <div id="progress-wrapper" style="background: #eef2f5; border-radius: 4px; overflow: hidden; height: 25px; margin-top: 20px; border: 1px solid #ccc; display: none;">
                    <div id="progress-bar" style="background: var(--verde-ok); height: 100%; width: 0%; transition: width 0.3s ease;"></div>
                </div>
                <div id="progress-text" style="margin-top: 8px; font-size: 0.95em; color: var(--azul-postal); font-weight: bold; display: none; text-align: center;">Iniciando...</div>
            </form>
        </div>

        <div class="card" style="border-top: 4px solid var(--amarelo-postal);">
            <h3 style="margin-top:0; color: var(--azul-postal);">Gerenciador de Tabelas e Módulos</h3>
            <table>
                <thead><tr><th>Tabela / Competência</th><th>Status</th><th>Linhas</th><th>Ação</th></tr></thead>
                <tbody>
                    {% for t_id, info in status_bases.items() %}
                        {% if t_id == 'faturamento' %}
                            <tr style="background: #f0f4f8;">
                                <td><strong>Faturamento Geral</strong></td>
                                <td style="color:{% if info.linhas > 0 %}var(--verde-ok){% else %}#999{% endif %}; font-weight:bold;">{{ info.status }}</td>
                                <td><strong>{{ info.linhas }}</strong></td>
                                <td>
                                    {% if info.linhas > 0 %}
                                    <form action="/admin/limpar/{{ t_id }}" method="post" onsubmit="return confirm('Deseja apagar TODO o faturamento de todos os meses? Isso limpará arquivos antigos também.');" style="margin:0;"><button type="submit" class="btn-danger" style="padding: 3px 8px; font-size: 0.85em;">Limpar Tudo</button></form>
                                    {% endif %}
                                </td>
                            </tr>
                            
                            {% for c in comps_fat %}
                            <tr style="background: #ffffff; font-size: 0.9em;">
                                <td style="padding-left: 25px; color: var(--azul-claro);">└─ Mês Salvo: <strong>{{ c.comp }}</strong></td>
                                <td style="color: #666; font-style: italic;">Ativa</td>
                                <td>{{ c.linhas }}</td>
                                <td>
                                    <form action="/admin/limpar_competencia/{{ c.comp }}" method="post" onsubmit="return confirm('Tem certeza que deseja apagar apenas o mês {{ c.comp }}?');" style="margin:0;">
                                        <button type="submit" class="btn-danger" style="background:#e67e22; padding: 3px 8px; font-size: 0.85em;">Excluir Mês</button>
                                    </form>
                                </td>
                            </tr>
                            {% endfor %}
                            
                        {% else %}
                            <tr>
                                <td><strong>{{ info.desc }}</strong></td>
                                <td style="color:{% if info.linhas > 0 %}var(--verde-ok){% else %}#999{% endif %}; font-weight:bold;">{{ info.status }}</td>
                                <td>{{ info.linhas }}</td>
                                <td>
                                    {% if info.linhas > 0 %}
                                    <form action="/admin/limpar/{{ t_id }}" method="post" onsubmit="return confirm('Apagar a tabela {{ info.desc }}?');" style="margin:0;"><button type="submit" class="btn-danger" style="padding: 3px 8px; font-size: 0.85em;">Limpar</button></form>
                                    {% endif %}
                                </td>
                            </tr>
                        {% endif %}
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>

<script>
    const form = document.getElementById('upload-form');
    form.addEventListener('submit', function(e) {
        e.preventDefault();
        const formData = new FormData(form);
        const xhr = new XMLHttpRequest();
        
        document.getElementById('progress-wrapper').style.display = 'block';
        document.getElementById('progress-text').style.display = 'block';
        const btn = document.getElementById('upload-btn');
        btn.disabled = true;
        btn.innerText = 'Transmissão em andamento...';
        btn.style.backgroundColor = '#999';

        xhr.upload.addEventListener('progress', function(e) {
            if (e.lengthComputable) {
                const percent = Math.round((e.loaded / e.total) * 100);
                document.getElementById('progress-bar').style.width = percent + '%';
                if (percent < 100) { document.getElementById('progress-text').innerText = 'Enviando arquivos: ' + percent + '%'; } 
                else {
                    document.getElementById('progress-text').innerText = 'Upload 100%! O BD está processando em lotes de 200.000 (Aguarde)...';
                    document.getElementById('progress-bar').style.backgroundColor = 'var(--amarelo-postal)';
                }
            }
        });
        xhr.onload = function() {
            if (xhr.status === 200) { document.open(); document.write(xhr.responseText); document.close(); } 
            else { alert('Erro na resposta do servidor.'); window.location.reload(); }
        };
        xhr.open('POST', '/admin_upload', true);
        xhr.send(formData);
    });
</script>
"""

# =====================================================================
# AUXILIARES DE CÁLCULO FINANCEIRO E EXTRATOR DE ITENS
# =====================================================================
def aplicar_reajustes_simulados(df_cruzado, f):
    if df_cruzado.empty:
        return df_cruzado
        
    df = df_cruzado.copy()
    v_col = _find_column(df, [COL_VALOR_PAGO, 'VALOR_PAG', 'VALOR_PAGO', 'VALOR'])
    if not v_col: return df

    # Filtros de Região / Negociação
    if f['tipo_neg'] == 'ESTADO' and f['uf_alvo']:
        df = df[df['UF'].fillna('').astype(str).str.upper() == f['uf_alvo'].upper()]
    elif f['tipo_neg'] == 'DIFERENCIADA' and f['cnpj_alvo']:
        if f['busca_por'] == 'CNPJ':
            df = df[df['CNPJ'].fillna('').astype(str).str.contains(f['cnpj_alvo'], na=False)]
        else:
            mask_nome = df['NOME_FANTASIA_PRESTADOR'].fillna('').astype(str).str.contains(f['cnpj_alvo'], na=False, case=False)
            mask_cnpj = df['CNPJ'].fillna('').astype(str).str.contains(f['cnpj_alvo'], na=False)
            df = df[mask_nome | mask_cnpj]
    elif f['tipo_neg'] == 'MISTO':
        if f['uf_alvo']: df = df[df['UF'].fillna('').astype(str).str.upper() == f['uf_alvo'].upper()]
        if f['cnpj_alvo']:
            if f['busca_por'] == 'CNPJ': df = df[df['CNPJ'].fillna('').astype(str).str.contains(f['cnpj_alvo'], na=False)]
            else:
                mask_nome = df['NOME_FANTASIA_PRESTADOR'].fillna('').astype(str).str.contains(f['cnpj_alvo'], na=False, case=False)
                mask_cnpj = df['CNPJ'].fillna('').astype(str).str.contains(f['cnpj_alvo'], na=False)
                df = df[mask_nome | mask_cnpj]

    df['VALOR_BASE'] = pd.to_numeric(df[v_col], errors='coerce').fillna(0)
    
    # Criamos a coluna final que vai separar as coisas pro PDF/Tabela
    df['ORIGEM_CALCULO'] = df['ORIGEM_INICIAL']
    df['TAXA_SOLICITADA'] = 0.0
    df['TAXA_CONCEDIDA'] = 0.0

    if f['modo_aplicacao'] == 'LINEAR':
        df['TAXA_SOLICITADA'] = f['sol_linear']
        df['TAXA_CONCEDIDA'] = f['conc_linear']
        df['ORIGEM_CALCULO'] = 'Modo Linear'
    else:
        # 1. Aplica regra geral base
        df['TAXA_SOLICITADA'] = np.where(df['ORIGEM_INICIAL'] == 'Dotação', f['sol_dotacao'], f['sol_faixa'])
        df['TAXA_CONCEDIDA'] = np.where(df['ORIGEM_INICIAL'] == 'Dotação', f['conc_dotacao'], f['conc_faixa'])

        # 2. O Extrator (Itens Específicos tiram os itens da regra geral se foram preenchidos)
        especificos = {
            'DIETAS': ('sol_dietas', 'conc_dietas'), 'PERFUROCORTANTES': ('sol_perfuro', 'conc_perfuro'),
            'ANESTESISTA': ('sol_anest', 'conc_anest'), 'MATERIAIS': ('sol_mat', 'conc_mat'),
            'MEDICAMENTOS': ('sol_med', 'conc_med'), 'DIARIAS': ('sol_dia', 'conc_dia'),
            'TAXAS': ('sol_taxa', 'conc_taxa'), 'GASES': ('sol_gas', 'conc_gas'),
            'OPME': ('sol_opme', 'conc_opme'), 'SADT': ('sol_sadt', 'conc_sadt'),
            'HONORARIOS': ('sol_hon', 'conc_hon'), 'OUTROS': ('sol_outros', 'conc_outros')
        }

        for grupo, (k_sol, k_conc) in especificos.items():
            val_sol = f[k_sol]
            val_conc = f[k_conc]
            # Se o usuário digitou qualquer coisa diferente de 0.0, a gente arranca o item da regra geral
            if val_sol != 0.0 or val_conc != 0.0:
                mask = df['TIPO_DESPESA_FINAL'] == grupo
                if mask.any():
                    df.loc[mask, 'TAXA_SOLICITADA'] = val_sol
                    df.loc[mask, 'TAXA_CONCEDIDA'] = val_conc
                    df.loc[mask, 'ORIGEM_CALCULO'] = 'Item Específico'

    # Cálculos Matemáticos Finais
    df['VALOR_SOLICITADO'] = df['VALOR_BASE'] * (1 + (df['TAXA_SOLICITADA'] / 100))
    df['VALOR_CONCEDIDO'] = df['VALOR_BASE'] * (1 + (df['TAXA_CONCEDIDA'] / 100))
    df['CUSTO_EVITADO'] = df['VALOR_SOLICITADO'] - df['VALOR_CONCEDIDO']
    
    return df

def processa_filtros_request(req):
    keys_float = [
        'sol_dotacao', 'conc_dotacao', 'sol_faixa', 'conc_faixa', 'sol_linear', 'conc_linear',
        'sol_dietas', 'conc_dietas', 'sol_perfuro', 'conc_perfuro', 'sol_anest', 'conc_anest',
        'sol_mat', 'conc_mat', 'sol_med', 'conc_med', 'sol_dia', 'conc_dia', 'sol_taxa', 'conc_taxa',
        'sol_gas', 'conc_gas', 'sol_opme', 'conc_opme', 'sol_sadt', 'conc_sadt', 'sol_hon', 'conc_hon',
        'sol_outros', 'conc_outros', 'ipca'
    ]
    f = {k: float(req.args.get(k, '0.00') or 0.0) for k in keys_float}
    f.update({
        'comp': req.args.get('comp', '').strip(),
        'modo_aplicacao': req.args.get('modo_aplicacao', 'POR_TIPO'),
        'analista': req.args.get('analista', '').strip(),
        'gestor': req.args.get('gestor', '').strip(),
        'tipo_neg': req.args.get('tipo_neg', 'TODOS').strip(),
        'uf_alvo': req.args.get('uf_alvo', '').strip(),
        'cnpj_alvo': req.args.get('cnpj_alvo', '').strip(),
        'busca_por': req.args.get('busca_por', 'CNPJ').strip()
    })
    return f

# =====================================================================
# ROTAS FLASK
# =====================================================================

@app.route('/Logo_Postal-03.png')
def serve_logo():
    return send_from_directory(os.getcwd(), 'Logo_Postal-03.png')

@app.route('/')
def dashboard():
    f = processa_filtros_request(request)
    totais = {'faturamento_total': '0,00', 'total_solicitado': '0,00', 'linhas_faturamento': 0, 'total_concedido': '0,00', 'custo_evitado': '0,00'}
    itens_detalhe = []
    tem_dados = False
    
    if f['comp'] and engine:
        try:
            df_fat = pd.read_sql(text(f"SELECT * FROM faturamento WHERE \"COMPETENCIA\" = '{f['comp']}'"), con=engine)
            if not df_fat.empty:
                try: df_mat = pd.read_sql(text("SELECT * FROM materiais"), con=engine)
                except: df_mat = pd.DataFrame()
                try: df_die = pd.read_sql(text("SELECT * FROM dietas"), con=engine)
                except: df_die = pd.DataFrame()
                try: df_dot = pd.read_sql(text("SELECT * FROM dotacoes"), con=engine)
                except: df_dot = pd.DataFrame()
                try: df_fai = pd.read_sql(text("SELECT * FROM faixas"), con=engine)
                except: df_fai = pd.DataFrame()
                try: df_pre = pd.read_sql(text("SELECT * FROM prestadores"), con=engine)
                except: df_pre = pd.DataFrame()

                df_cruzado = cruzar_bases(df_fat, df_mat, df_die, df_dot, df_fai, df_pre)
                df_final = aplicar_reajustes_simulados(df_cruzado, f)
                
                if not df_final.empty:
                    tem_dados = True
                    fat_total = df_final['VALOR_BASE'].sum()
                    sol_total = df_final['VALOR_SOLICITADO'].sum()
                    con_total = df_final['VALOR_CONCEDIDO'].sum()
                    evit_total = df_final['CUSTO_EVITADO'].sum()

                    totais = {
                        'faturamento_total': f"{fat_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                        'total_solicitado': f"{sol_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                        'linhas_faturamento': len(df_final),
                        'total_concedido': f"{con_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                        'custo_evitado': f"{evit_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    }

                    # AGRUPAMENTO FINAL (Usa a ORIGEM_CALCULO para separar os itens extraídos)
                    grupo = df_final.groupby(['TIPO_DESPESA_FINAL', 'ORIGEM_CALCULO']).agg(
                        qtd=('VALOR_BASE', 'count'), v_base=('VALOR_BASE', 'sum'),
                        v_sol=('VALOR_SOLICITADO', 'sum'), v_con=('VALOR_CONCEDIDO', 'sum')
                    ).reset_index()

                    for _, r in grupo.iterrows():
                        itens_detalhe.append({
                            'tipo_despesa': r['TIPO_DESPESA_FINAL'],
                            'origem': r['ORIGEM_CALCULO'],
                            'qtd': r['qtd'],
                            'valor_base': f"{r['v_base']:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                            'valor_sol': f"{r['v_sol']:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                            'valor_con': f"{r['v_con']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                        })
        except Exception as e:
            flash(f"Erro ao processar SQL: {str(e)}", "error")

    return render_template_string(HTML_DASHBOARD, totais=totais, itens_detalhe=itens_detalhe, periodo_base=f['comp'] or "Nenhuma", filtros=f, tem_dados=tem_dados)

@app.route('/exportar')
def exportar():
    f = processa_filtros_request(request)
    if not f['comp'] or not engine: return redirect(url_for('dashboard'))
    
    try:
        df_fat = pd.read_sql(text(f"SELECT * FROM faturamento WHERE \"COMPETENCIA\" = '{f['comp']}'"), con=engine)
        if df_fat.empty: return redirect(url_for('dashboard'))
        try: df_mat = pd.read_sql(text("SELECT * FROM materiais"), con=engine)
        except: df_mat = pd.DataFrame()
        try: df_die = pd.read_sql(text("SELECT * FROM dietas"), con=engine)
        except: df_die = pd.DataFrame()
        try: df_dot = pd.read_sql(text("SELECT * FROM dotacoes"), con=engine)
        except: df_dot = pd.DataFrame()
        try: df_fai = pd.read_sql(text("SELECT * FROM faixas"), con=engine)
        except: df_fai = pd.DataFrame()
        try: df_pre = pd.read_sql(text("SELECT * FROM prestadores"), con=engine)
        except: df_pre = pd.DataFrame()
        
        df_cruzado = cruzar_bases(df_fat, df_mat, df_die, df_dot, df_fai, df_pre)
        df_final = aplicar_reajustes_simulados(df_cruzado, f)
        
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df_final.to_excel(writer, index=False, sheet_name="Reajuste")
        output.seek(0)
        return send_file(output, download_name=f"Reajuste_{f['comp']}.xlsx", as_attachment=True)
    except Exception: return redirect(url_for('dashboard'))

@app.route('/exportar_pdf')
def exportar_pdf():
    f = processa_filtros_request(request)
    if not f['comp'] or not engine: return redirect(url_for('dashboard'))
    
    try:
        df_fat = pd.read_sql(text(f"SELECT * FROM faturamento WHERE \"COMPETENCIA\" = '{f['comp']}'"), con=engine)
        if df_fat.empty: return redirect(url_for('dashboard'))
        try: df_mat = pd.read_sql(text("SELECT * FROM materiais"), con=engine)
        except: df_mat = pd.DataFrame()
        try: df_die = pd.read_sql(text("SELECT * FROM dietas"), con=engine)
        except: df_die = pd.DataFrame()
        try: df_dot = pd.read_sql(text("SELECT * FROM dotacoes"), con=engine)
        except: df_dot = pd.DataFrame()
        try: df_fai = pd.read_sql(text("SELECT * FROM faixas"), con=engine)
        except: df_fai = pd.DataFrame()
        try: df_pre = pd.read_sql(text("SELECT * FROM prestadores"), con=engine)
        except: df_pre = pd.DataFrame()
        
        df_cruzado = cruzar_bases(df_fat, df_mat, df_die, df_dot, df_fai, df_pre)
        df_final = aplicar_reajustes_simulados(df_cruzado, f)
        
        grupo = df_final.groupby(['TIPO_DESPESA_FINAL', 'ORIGEM_CALCULO']).agg(
            v_base=('VALOR_BASE', 'sum'), v_sol=('VALOR_SOLICITADO', 'sum'), v_con=('VALOR_CONCEDIDO', 'sum')
        ).reset_index()

        by_type = []
        for _, r in grupo.iterrows():
            by_type.append({
                'tipo': r['TIPO_DESPESA_FINAL'], 'origem': r['ORIGEM_CALCULO'],
                'valor': float(r['v_base']), 'delta_solicitado': float(r['v_sol']), 'delta_concedido': float(r['v_con'])
            })

        data_pdf = {
            'comp': f['comp'], 'uf_alvo': f['uf_alvo'], 'cnpj_alvo': f['cnpj_alvo'],
            'busca_por': f['busca_por'], 'modo_aplicacao': f['modo_aplicacao'],
            'ipca': f['ipca'], 'tipo_neg': f['tipo_neg'], 'analista': f['analista'], 'gestor': f['gestor'],
            'by_type': by_type,
            'totals': {
                'total_faturamento': float(df_final['VALOR_BASE'].sum()), 'total_solicited': float(df_final['VALOR_SOLICITADO'].sum()),
                'total_conceded': float(df_final['VALOR_CONCEDIDO'].sum()), 'custo_evitado': float(df_final['CUSTO_EVITADO'].sum())
            }
        }
        
        pdf_bytes = build_analysis_pdf_bytes(data_pdf)
        response = make_response(pdf_bytes)
        response.headers.set('Content-Type', 'application/pdf')
        response.headers.set('Content-Disposition', f'attachment; filename="Relatorio_Impacto_{f["comp"]}.pdf"')
        return response
    except Exception as e:
        flash(f"Erro ao gerar PDF: {str(e)}", "error")
        return redirect(url_for('dashboard'))

@app.route('/admin')
def admin(): 
    status = obter_linhas_tabela()
    comps_fat = []
    if engine:
        try:
            with engine.connect() as conn:
                try:
                    res = conn.execute(text("SELECT \"COMPETENCIA\", COUNT(*) FROM faturamento GROUP BY \"COMPETENCIA\" ORDER BY \"COMPETENCIA\" DESC"))
                    for row in res: comps_fat.append({'comp': row[0], 'linhas': row[1]})
                except: pass
        except: pass
    return render_template_string(HTML_ADMIN, status_bases=status, comps_fat=comps_fat)

@app.route('/admin/limpar/<tipo_base>', methods=['POST'])
def limpar_base(tipo_base):
    if engine:
        try:
            with engine.begin() as conn: conn.execute(text(f"DROP TABLE IF EXISTS {tipo_base}"))
            flash(f"Módulo [{tipo_base}] resetado com sucesso!", "success")
        except Exception as e: flash(f"Erro ao limpar base: {str(e)}", "error")
    return redirect(url_for('admin'))

@app.route('/admin/limpar_competencia/<comp>', methods=['POST'])
def limpar_competencia(comp):
    if engine:
        try:
            with engine.begin() as conn: conn.execute(text(f"DELETE FROM faturamento WHERE \"COMPETENCIA\" = '{comp}'"))
            flash(f"Competência [{comp}] excluída cirurgicamente!", "success")
        except Exception as e: flash(f"Erro: {str(e)}", "error")
    return redirect(url_for('admin'))

@app.route('/admin_upload', methods=['POST'])
def admin_upload():
    arquivos = request.files.getlist('arquivos_pasta') + request.files.getlist('arquivos_soltos')
    tipo_base = request.form.get('tipo_base')
    competencia = request.form.get('competencia')
    if not arquivos or all(a.filename == '' for a in arquivos):
        flash("Nenhum arquivo válido foi enviado!", "error")
        return redirect(url_for('admin'))

    linhas = 0
    primeiro = True
    try:
        for arquivo in arquivos:
            if arquivo.filename == '': continue
            nome_arquivo_puro = os.path.splitext(os.path.basename(arquivo.filename))[0]
            if arquivo.filename.endswith('.parquet'): df = pd.read_parquet(arquivo)
            elif arquivo.filename.endswith('.csv') or arquivo.filename.endswith('.txt'):
                amostra = arquivo.read(2048).decode('utf-8', errors='ignore')
                arquivo.seek(0)
                delimitador = '¬' if '¬' in amostra else ';' if ';' in amostra else '\t' if '\t' in amostra else None
                try: df = pd.read_csv(arquivo, sep=delimitador, engine='python', encoding='utf-8', on_bad_lines='skip')
                except: 
                    arquivo.seek(0)
                    df = pd.read_csv(arquivo, sep=delimitador, engine='python', encoding='iso-8859-1', on_bad_lines='skip')
            elif arquivo.filename.endswith('.xlsx'): df = pd.read_excel(arquivo)
            else: continue

            if df.empty: continue
            for col in ['UF', 'AP', 'CNPJ']:
                if col not in df.columns: df[col] = None
            
            if tipo_base == 'faturamento':
                comp_final = competencia if competencia else nome_arquivo_puro
                df['COMPETENCIA'] = str(comp_final).strip()

            if 'VLR_DESCONTO_OBTIDO' in df.columns:
                df['VLR_DESCONTO_OBTIDO'] = pd.to_numeric(df['VLR_DESCONTO_OBTIDO'], errors='coerce').fillna(0)
                tot = df['VLR_DESCONTO_OBTIDO'].sum()
                df['VLR_DESCONTO_OBTIDO'] = 0.0
                df.at[df.index[0], 'VLR_DESCONTO_OBTIDO'] = tot

            with engine.begin() as conn:
                if tipo_base == 'faturamento':
                    try: conn.execute(text('ALTER TABLE faturamento ADD COLUMN IF NOT EXISTS "COMPETENCIA" TEXT;'))
                    except: pass
                    df.to_sql('faturamento', con=conn, if_exists='append', index=False, chunksize=200000)
                else:
                    df.to_sql(tipo_base, con=conn, if_exists='replace' if primeiro else 'append', index=False, chunksize=200000)
            linhas += len(df)
            primeiro = False
        flash(f"Sucesso! {linhas} linhas gravadas em [{tipo_base}].", "success")
    except Exception as e: flash(f"Erro: {str(e)}", "error")
    return redirect(url_for('admin'))
