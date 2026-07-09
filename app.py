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

TIPOS_DESPESA = [
    ('ANESTESISTA', 'anest'), ('CONSULTAS / HONORÁRIOS', 'hon'), ('DIÁRIAS', 'dia'),
    ('DIETAS', 'dietas'), ('GASES MEDICINAIS', 'gas'), ('MATERIAIS', 'mat'),
    ('MEDICAMENTOS', 'med'), ('OPME', 'opme'), ('PERFUROCORTANTES', 'perfuro'),
    ('SADT / EXAMES', 'sadt'), ('TAXAS', 'taxa'), ('OUTROS / GERAL', 'outros')
]

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

# --- FAREJADOR BLINDADO (Com Cruzamento com Base de Prestadores) ---
def ler_e_filtrar_faturamento(engine, comp_list, f):
    # PRE-CRUZAMENTO: Busca o Prestador na Base de Cadastro primeiro!
    prestadores_encontrados = []
    nomes_encontrados = []
    cnpj_formatado = re.sub(r'\D', '', str(f.get('cnpj_alvo', '')))
    nome_alvo = str(f.get('cnpj_alvo', '')).strip().upper()

    try:
        # Se for busca Diferenciada/Mista, caçamos o cara na base de prestadores
        if f['tipo_neg'] in ['DIFERENCIADA', 'MISTO'] and f['cnpj_alvo']:
            df_pre_temp = pd.read_sql(text("SELECT * FROM prestadores"), con=engine)
            if not df_pre_temp.empty:
                # Padroniza as colunas de busca do Prestador
                col_cnpj_pre = _find_column(df_pre_temp, ["CPFCNPJ", "CNPJ", "CGCCPF", "PRESTADOR"])
                col_nome_pre = _find_column(df_pre_temp, ["NOM", "NOME_FANTASIA", "RAZAO_SOCIAL", "NOMEPRESTADOR", "FANTASIA"])
                
                if col_cnpj_pre:
                    df_pre_temp['CNPJ_LIMPO'] = df_pre_temp[col_cnpj_pre].fillna("").astype(str).str.replace(r'\D', '', regex=True)
                if col_nome_pre:
                    df_pre_temp['NOME_LIMPO'] = df_pre_temp[col_nome_pre].fillna("").astype(str).str.upper()

                # Filtra na base de prestadores
                if f['busca_por'] == 'CNPJ' and col_cnpj_pre:
                    mask = df_pre_temp['CNPJ_LIMPO'].str.contains(cnpj_formatado, na=False)
                    prestadores_encontrados = df_pre_temp.loc[mask, 'CNPJ_LIMPO'].unique().tolist()
                elif f['busca_por'] != 'CNPJ':
                    mask_n = df_pre_temp['NOME_LIMPO'].str.contains(nome_alvo, na=False) if col_nome_pre else pd.Series(False, index=df_pre_temp.index)
                    mask_c = df_pre_temp['CNPJ_LIMPO'].str.contains(cnpj_formatado, na=False) if col_cnpj_pre else pd.Series(False, index=df_pre_temp.index)
                    prestadores_encontrados = df_pre_temp.loc[mask_n | mask_c, 'CNPJ_LIMPO'].unique().tolist()
                    if col_nome_pre:
                        nomes_encontrados = df_pre_temp.loc[mask_n | mask_c, 'NOME_LIMPO'].unique().tolist()
    except:
        pass # Segue o jogo se falhar e tenta achar direto no faturamento

    # CARREGAMENTO DO FATURAMENTO EM LOTES
    format_strings = ','.join([f"'{c}'" for c in comp_list])
    sql = f"SELECT * FROM faturamento WHERE \"COMPETENCIA\" IN ({format_strings})"
    
    df_list = []
    for chunk in pd.read_sql(text(sql), con=engine, chunksize=100000):
        
        # Unifica CNPJ do faturamento (para bater com a busca que fizemos em Prestadores)
        chunk["CNPJ_FILTRO"] = ""
        for cand in ["PRESTADOR", "CNPJ_EXECUTOR", "CNPJ", "CGCCPF", "CPFCNPJ"]:
            c = _find_column(chunk, [cand])
            if c:
                mask = chunk["CNPJ_FILTRO"] == ""
                chunk.loc[mask, "CNPJ_FILTRO"] = chunk.loc[mask, c].fillna("").astype(str)
        chunk['CNPJ_LIMPO'] = chunk['CNPJ_FILTRO'].str.replace(r'\D', '', regex=True)

        # Unifica Nomes
        chunk["NOME_FILTRO"] = ""
        for cand in ["NOME_FANTASIA_PRESTADOR", "NOMEPRESTADOR", "RAZAO_SOCIAL", "NOME_FANTASIA", "PRESTADOR_NOME", "EXECUTOR"]:
            c = _find_column(chunk, [cand])
            if c:
                mask = chunk["NOME_FILTRO"] == ""
                chunk.loc[mask, "NOME_FILTRO"] = chunk.loc[mask, c].fillna("").astype(str).str.upper()

        # Unifica UF
        chunk["UF_FILTRO"] = ""
        for cand in ["UF", "ESTADO", "UF_PRESTADOR", "FILIALBENEFICIARIO", "FILIAL_EXECUTOR", "FILIAL"]:
            c = _find_column(chunk, [cand])
            if c:
                mask = chunk["UF_FILTRO"] == ""
                chunk.loc[mask, "UF_FILTRO"] = chunk.loc[mask, c].fillna("").astype(str)

        # Aplica o Filtro Final
        if f['tipo_neg'] == 'ESTADO' and f['uf_alvo']:
            chunk = chunk[chunk['UF_FILTRO'].str.upper().str.contains(f['uf_alvo'].upper(), na=False)]
            
        elif f['tipo_neg'] == 'DIFERENCIADA' and f['cnpj_alvo']:
            # Se achou na base de Prestadores, cruza e filtra exato!
            if prestadores_encontrados or nomes_encontrados:
                mask_pre_cnpj = chunk['CNPJ_LIMPO'].isin(prestadores_encontrados)
                mask_pre_nome = chunk['NOME_FILTRO'].isin(nomes_encontrados)
                
                # Mas também tenta achar direto no faturamento como fallback de segurança
                mask_direta_cnpj = chunk['CNPJ_LIMPO'].str.contains(cnpj_formatado, na=False)
                mask_direta_nome = chunk['NOME_FILTRO'].str.contains(nome_alvo, na=False)
                
                chunk = chunk[mask_pre_cnpj | mask_pre_nome | mask_direta_cnpj | mask_direta_nome]
            else:
                # Se não tem base de prestadores, vai na força bruta do faturamento
                if f['busca_por'] == 'CNPJ': 
                    chunk = chunk[chunk['CNPJ_LIMPO'].str.contains(cnpj_formatado, na=False)]
                else:
                    mask_nome = chunk['NOME_FILTRO'].str.contains(nome_alvo, na=False)
                    mask_cnpj = chunk['CNPJ_LIMPO'].str.contains(cnpj_formatado, na=False)
                    chunk = chunk[mask_nome | mask_cnpj]
                
        elif f['tipo_neg'] == 'MISTO':
            if f['uf_alvo']: 
                chunk = chunk[chunk['UF_FILTRO'].str.upper().str.contains(f['uf_alvo'].upper(), na=False)]
            if f['cnpj_alvo']:
                if prestadores_encontrados or nomes_encontrados:
                    mask_pre_cnpj = chunk['CNPJ_LIMPO'].isin(prestadores_encontrados)
                    mask_pre_nome = chunk['NOME_FILTRO'].isin(nomes_encontrados)
                    mask_direta_cnpj = chunk['CNPJ_LIMPO'].str.contains(cnpj_formatado, na=False)
                    mask_direta_nome = chunk['NOME_FILTRO'].str.contains(nome_alvo, na=False)
                    chunk = chunk[mask_pre_cnpj | mask_pre_nome | mask_direta_cnpj | mask_direta_nome]
                else:
                    if f['busca_por'] == 'CNPJ': 
                        chunk = chunk[chunk['CNPJ_LIMPO'].str.contains(cnpj_formatado, na=False)]
                    else:
                        mask_nome = chunk['NOME_FILTRO'].str.contains(nome_alvo, na=False)
                        mask_cnpj = chunk['CNPJ_LIMPO'].str.contains(cnpj_formatado, na=False)
                        chunk = chunk[mask_nome | mask_cnpj]
        
        if not chunk.empty:
            df_list.append(chunk)
            
    if df_list:
        return pd.concat(df_list, ignore_index=True)
    return pd.DataFrame()

def cruzar_bases(df_fat, df_mat, df_die, df_dot, df_fai, df_pre):
    try:
        df = df_fat.copy()
        
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
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("DIARIA", na=False), "TIPO_DESPESA_FINAL"] = "DIÁRIAS"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("TAXA", na=False), "TIPO_DESPESA_FINAL"] = "TAXAS"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("GASES", na=False), "TIPO_DESPESA_FINAL"] = "GASES MEDICINAIS"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("OPME|ORTESE|PROTESE", na=False), "TIPO_DESPESA_FINAL"] = "OPME"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("SADT|EXAME|DIAGNOSTICO|IMAGEM", na=False), "TIPO_DESPESA_FINAL"] = "SADT / EXAMES"
            df.loc[df["TIPO_DESPESA_FINAL"].str.contains("HONORARIO|CONSULTA|VISITA|MEDICO", na=False), "TIPO_DESPESA_FINAL"] = "CONSULTAS / HONORÁRIOS"
        else:
            df["TIPO_DESPESA_FINAL"] = "OUTROS / GERAL"
        
        if grau_col:
            mask_anestesista = df[grau_col].fillna("").astype(str).str.upper().isin(["ANESTESISTA", "AUXILIAR DE ANESTESISTA"])
            df.loc[mask_anestesista, "TIPO_DESPESA_FINAL"] = "ANESTESISTA"

        df.loc[df["_COD_LIMPO_"].isin(s_diet) & (df["_COD_LIMPO_"] != ""), "TIPO_DESPESA_FINAL"] = "DIETAS"
        df.loc[df["_COD_LIMPO_"].isin(s_perf) & (df["_COD_LIMPO_"] != "") & (df["TIPO_DESPESA_FINAL"] != "DIETAS"), "TIPO_DESPESA_FINAL"] = "PERFUROCORTANTES"
        
        return df.drop(columns=["CHAVE"], errors="ignore")
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
    pdf.cell(0, 7, f"Competencia(s) Analisada(s): {', '.join(data.get('comp_list', []))}", 0, 1, "L")
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
        pdf.cell(0, 10, f">> ORIGEM: {cat.upper()}", 0, 1, "L")
        
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
# HTML DASHBOARD (Com Controle de Fases)
# =====================================================================
CSS_PADRAO = """
<style>
    :root { --azul-postal: #002c52; --azul-claro: #005a92; --amarelo-postal: #f9b200; --verde-ok: #007a33; --vermelho-alerta: #cc0000; --fundo: #f4f7f6; }
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--fundo); color: #333; margin: 0; display: flex; min-height: 100vh; }
    
    .sidebar { width: 280px; background-color: white; border-right: 1px solid #ddd; padding: 20px; display: flex; flex-direction: column; gap: 20px; box-shadow: 2px 0 5px rgba(0,0,0,0.05); z-index: 10; }
    .main-content { flex: 1; display: flex; flex-direction: column; overflow-y: auto; }
    
    .logo-img { max-width: 200px; height: auto; object-fit: contain; margin-bottom: 10px; }
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

    .tabs { display: flex; border-bottom: 2px solid #ddd; margin-bottom: 20px; gap: 5px; }
    .tab-link { padding: 12px 25px; cursor: pointer; font-weight: bold; color: #666; border-bottom: 3px solid transparent; transition: 0.3s; font-size: 0.95em; background: none; border-top: none; border-left: none; border-right: none; outline: none; }
    .tab-link:hover { color: var(--azul-claro); }
    .tab-link.active { color: var(--vermelho-alerta); border-bottom-color: var(--vermelho-alerta); }
    .tab-content { display: none; animation: fadeIn 0.3s; }
    .tab-content.active { display: block; }

    .expense-row { display: flex; justify-content: space-between; align-items: center; padding: 12px; border-bottom: 1px solid #eee; transition: background 0.2s; }
    .expense-row:hover { background-color: #f9f9f9; }
    .expense-label { font-weight: bold; color: #333; font-size: 0.9em; flex: 1; }
    .expense-value { font-weight: normal; color: #666; margin-left: 5px; }
    .expense-inputs { display: flex; gap: 15px; align-items: center; }
    .input-wrapper { display: flex; flex-direction: column; align-items: flex-start; }
    .input-wrapper label { font-size: 0.75em; color: #888; font-weight: normal; margin-bottom: 2px; }
    .input-wrapper input { width: 100px; padding: 6px; border: 1px solid #ccc; border-radius: 4px; text-align: right; background: #fdfdfd; }

    .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; }
    .metric-box { padding: 15px; border: 1px solid #eee; border-radius: 6px; text-align: center; background: #fafafa; }
    .metric-box h4 { margin: 0 0 10px 0; color: #666; font-size: 0.85em; text-transform: uppercase; }
    .metric-box .valor { font-size: 1.6em; font-weight: bold; color: var(--azul-postal); margin-bottom: 5px; }
    .metric-box .sub { font-size: 0.85em; color: #888; }
    .impacto-card { background-color: #fff9e6; border-top: 4px solid var(--amarelo-postal); }
    
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
            if (divLinear) divLinear.style.display = 'block';
        } else {
            divTipo.style.display = 'block';
            if (divLinear) divLinear.style.display = 'none';
        }
    }

    window.onload = function() {
        toggleFiltros();
        toggleModo();
    };
</script>
"""

HTML_DASHBOARD = CSS_PADRAO + """
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
                <label>Período da Análise (Selecione 1 ou mais)</label>
                <select name="comp" class="form-control" multiple size="4" required style="height:auto;">
                    {% for c in comps_disponiveis %}
                    <option value="{{ c }}" {% if c in filtros.comp_list %}selected{% endif %}>{{ c }}</option>
                    {% endfor %}
                </select>
                <small style="color:#888;">Segure CTRL para selecionar vários</small>
            </div>
        </div>

        <div class="sidebar-section">
            <h4>Exceções por Item Específico</h4>
            <div class="form-group">
                <label>Códigos dos Itens (Ex: 10101012)</label>
                <textarea name="itens_exc" class="form-control" rows="3" placeholder="Digite os códigos separados por vírgula. Eles serão extraídos da Faixa/Dotação.">{{ filtros.itens_exc }}</textarea>
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
        <button type="submit" name="step" value="1" class="btn" style="background-color: var(--amarelo-postal); color: var(--azul-postal); width: 100%; font-size: 1.1em; padding: 12px; margin-top: 10px;">Carregar e Cruzar Bases</button>
    </div>

    <div class="main-content">
        <div class="header">
            <h2>Sistema de reajuste de discussão</h2>
            <a href="/admin" class="btn" style="background:#eef2f5; color:var(--azul-postal); border:1px solid #ccc;">Gerenciar Banco de Dados</a>
        </div>

        <div class="container">
            {% with messages = get_flashed_messages(category_filter=["error"]) %}{% if messages %}{% for m in messages %}<div class="alert alert-danger">{{ m }}</div>{% endfor %}{% endif %}{% endwith %}

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
                            <label><input type="radio" name="busca_por" value="CNPJ" {% if filtros.busca_por != 'GRUPO' %}checked{% endif %}> CNPJ / EXECUTOR</label>
                            <label><input type="radio" name="busca_por" value="GRUPO" {% if filtros.busca_por == 'GRUPO' %}checked{% endif %}> GRUPO PRESTADOR / NOME</label>
                        </div>
                    </div>
                    <div class="form-group" style="max-width: 400px;">
                        <label>Digite o CNPJ ou Nome do Prestador:</label>
                        <input type="text" name="cnpj_alvo" class="form-control" value="{{ filtros.cnpj_alvo }}" placeholder="Digite o texto de busca...">
                    </div>
                </div>
            </div>

            {% if step == '1' or step == '2' %}
            <div class="card" id="div_por_tipo">
                <h3 style="margin-top:0; color: var(--azul-postal); margin-bottom: 20px;">Definição de Propostas por Origem</h3>
                <div class="tabs">
                    <button type="button" class="tab-link active" onclick="openTab(event, 'tab-dotacao')">Dotação</button>
                    <button type="button" class="tab-link" onclick="openTab(event, 'tab-faixa')">Faixa de Evento</button>
                    <button type="button" class="tab-link" onclick="openTab(event, 'tab-especificos')">Itens Específicos</button>
                </div>

                <div id="tab-dotacao" class="tab-content active">
                    <p style="color:#666; font-size:0.9em; margin-bottom:15px;">Itens identificados na base de <strong>Dotação</strong>.</p>
                    {% for label, key in tipos_despesa %}
                    <div class="expense-row">
                        <div class="expense-label">
                            {{ label }} 
                            {% if bases_dict and bases_dict.get('Dotação', {}).get(label, 0) > 0 %}
                                <span class="expense-value">— R$ {{ "{:,.2f}".format(bases_dict['Dotação'][label]).replace(',','X').replace('.',',').replace('X','.') }}</span>
                            {% else %}
                                <span class="expense-value" style="color:#ccc;">— R$ 0,00</span>
                            {% endif %}
                        </div>
                        <div class="expense-inputs">
                            <div class="input-wrapper"><label>% Sol.</label><input type="number" step="0.01" name="sol_dot_{{ key }}" value="{{ filtros['sol_dot_'~key] }}"></div>
                            <div class="input-wrapper"><label>% Conc.</label><input type="number" step="0.01" name="conc_dot_{{ key }}" value="{{ filtros['conc_dot_'~key] }}"></div>
                        </div>
                    </div>
                    {% endfor %}
                </div>

                <div id="tab-faixa" class="tab-content">
                    <p style="color:#666; font-size:0.9em; margin-bottom:15px;">Itens identificados como <strong>Faixa de Eventos</strong>.</p>
                    {% for label, key in tipos_despesa %}
                    <div class="expense-row">
                        <div class="expense-label">
                            {{ label }} 
                            {% if bases_dict and bases_dict.get('Faixa de Eventos', {}).get(label, 0) > 0 %}
                                <span class="expense-value">— R$ {{ "{:,.2f}".format(bases_dict['Faixa de Eventos'][label]).replace(',','X').replace('.',',').replace('X','.') }}</span>
                            {% else %}
                                <span class="expense-value" style="color:#ccc;">— R$ 0,00</span>
                            {% endif %}
                        </div>
                        <div class="expense-inputs">
                            <div class="input-wrapper"><label>% Sol.</label><input type="number" step="0.01" name="sol_fai_{{ key }}" value="{{ filtros['sol_fai_'~key] }}"></div>
                            <div class="input-wrapper"><label>% Conc.</label><input type="number" step="0.01" name="conc_fai_{{ key }}" value="{{ filtros['conc_fai_'~key] }}"></div>
                        </div>
                    </div>
                    {% endfor %}
                </div>

                <div id="tab-especificos" class="tab-content">
                    <p style="color:#cc0000; font-size:0.9em; font-weight:bold;">Itens extraídos a partir dos códigos informados na barra lateral.</p>
                    <div class="expense-row" style="background: #fff3cd; border: 1px solid #ffeeba;">
                        <div class="expense-label" style="color:#856404;">
                            TODOS OS CÓDIGOS ESPECÍFICOS EXTRAÍDOS
                            {% if bases_dict and bases_dict.get('Item Específico', {}).get('TOTAL', 0) > 0 %}
                                <span class="expense-value" style="color:#856404;">— R$ {{ "{:,.2f}".format(bases_dict['Item Específico']['TOTAL']).replace(',','X').replace('.',',').replace('X','.') }}</span>
                            {% else %}
                                <span class="expense-value" style="color:#ccc;">— R$ 0,00</span>
                            {% endif %}
                        </div>
                        <div class="expense-inputs">
                            <div class="input-wrapper"><label>% Sol.</label><input type="number" step="0.01" name="sol_exc" value="{{ filtros['sol_exc'] }}"></div>
                            <div class="input-wrapper"><label>% Conc.</label><input type="number" step="0.01" name="conc_exc" value="{{ filtros['conc_exc'] }}"></div>
                        </div>
                    </div>
                </div>
            </div>
            
            <div id="div_linear" style="display:none; background:#fde8e8; padding:20px; border-radius:8px; border:1px solid var(--vermelho-alerta); margin-bottom:20px;">
                <h3 style="margin: 0 0 5px 0; color: var(--vermelho-alerta);">Modo Linear Ativado</h3>
                <p style="font-size:0.9em; color:#666; margin-bottom:15px;">A mesma taxa será aplicada a todas as linhas do faturamento.</p>
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
            
            <div style="text-align: right; margin-bottom: 20px;">
                <button type="submit" name="step" value="2" class="btn btn-success" style="width: auto; padding: 15px 40px; font-size: 1.1em;">CALCULAR ANÁLISE DE IMPACTO</button>
            </div>
            {% endif %}

            {% if step == '2' %}
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <h3 style="margin:0; color: var(--azul-postal);">Resumo Financeiro Consolidado</h3>
                    {% if tem_dados %}
                    <div>
                        <a href="/exportar?{{ query_string }}" class="btn btn-success">📥 Excel</a>
                        <a href="/exportar_pdf?{{ query_string }}" class="btn btn-pdf" target="_blank">📄 PDF</a>
                    </div>
                    {% endif %}
                </div>
                
                <div class="grid-4" style="margin-top: 15px;">
                    <div class="metric-box">
                        <h4>Faturamento Lido (Base)</h4>
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
                        <div class="sub">Economia Gerada</div>
                    </div>
                </div>
            </div>

            <div class="card">
                <h3 style="margin-top:0; color: var(--azul-postal);">Detalhamento de Impacto</h3>
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
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            {% endif %}
        </div>
    </div>
</form>
"""

# =====================================================================
# HTML ADMIN (RESTAURADO PARA O PADRÃO ORIGINAL)
# =====================================================================
HTML_ADMIN = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
    <meta charset="UTF-8">
    <title>Administração - GERED</title>
    <style>
        :root { --azul-postal: #002c52; --amarelo-postal: #f9b200; --verde-ok: #007a33; --vermelho-alerta: #cc0000; --fundo: #eef2f5; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--fundo); color: #333; margin: 0; }
        .header { background-color: white; padding: 20px 40px; display: flex; justify-content: space-between; align-items: center; border-bottom: 3px solid var(--amarelo-postal); box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .logo-img { height: 60px; object-fit: contain; }
        .container { padding: 30px 40px; max-width: 1200px; margin: 0 auto; }
        .card { background: white; padding: 25px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 20px; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; font-weight: bold; color: var(--azul-postal); font-size: 0.9em; }
        .form-control { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; font-size: 1em; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th { background-color: var(--azul-postal); color: white; padding: 12px; text-align: left; border-bottom: 3px solid var(--amarelo-postal); }
        td { padding: 10px 12px; border-bottom: 1px solid #eee; }
        tr:hover { background-color: #f4f7f6; }
        .btn { background-color: var(--azul-postal); color: white; font-weight: bold; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
        .btn:hover { background-color: #001a33; }
        .btn-success { background-color: var(--verde-ok); color: white; }
        .btn-danger { background-color: var(--vermelho-alerta); color: white; border: none; padding: 5px 10px; border-radius: 4px; cursor: pointer; }
        .alert { padding: 15px; border-radius: 4px; margin-bottom: 20px; }
        .alert-success { background: #e6f4ea; color: var(--verde-ok); border: 1px solid var(--verde-ok); }
        .alert-danger { background: #fde8e8; color: var(--vermelho-alerta); border: 1px solid var(--vermelho-alerta); }
    </style>
</head>
<body>
<div class="header">
    <div style="display: flex; align-items: center; gap: 20px;">
        <img src="/Logo_Postal-03.png" class="logo-img" alt="Postal Saúde">
        <h2 style="margin:0; color: var(--azul-postal);">Administração de Banco de Dados</h2>
    </div>
    <a href="/" class="btn">Voltar ao Dashboard</a>
</div>

<div class="container">
    {% with messages = get_flashed_messages(category_filter=["success"]) %}{% if messages %}{% for message in messages %}<div class="alert alert-success">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}
    {% with messages = get_flashed_messages(category_filter=["error"]) %}{% if messages %}{% for message in messages %}<div class="alert alert-danger">{{ message }}</div>{% endfor %}{% endif %}{% endwith %}

    <div style="display: grid; grid-template-columns: 1fr 1.2fr; gap: 20px;">
        <div class="card" style="border-top: 4px solid var(--azul-postal);">
            <h3 style="margin-top:0; color: var(--azul-postal);">Upload Inteligente em Massa</h3>
            
            <form id="upload-form" action="/admin_upload" method="post" enctype="multipart/form-data">
                <div class="form-group">
                    <label>Selecione a Base de Destino:</label>
                    <select name="tipo_base" class="form-control" required>
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
                    <input type="text" name="competencia" class="form-control" placeholder="Deixe VAZIO para usar o nome do arquivo">
                </div>

                <div class="form-group">
                    <label>Selecione a PASTA INTEIRA com os arquivos:</label>
                    <input type="file" name="arquivos_pasta" class="form-control" style="background: #fafafa; border: 2px dashed #005a92;" webkitdirectory directory multiple>
                </div>
                
                <div class="form-group">
                    <label>OU Selecione arquivos soltos manualmente:</label>
                    <input type="file" name="arquivos_soltos" class="form-control" style="background: #fafafa; border: 2px dashed #999;" multiple>
                </div>
                
                <button type="submit" id="upload-btn" class="btn btn-success" style="width: 100%; font-size: 1.1em; padding: 12px;">Injetar Dados no Servidor</button>
                
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
                                    <form action="/admin/limpar/{{ t_id }}" method="post" onsubmit="return confirm('Apagar TODO o faturamento?');" style="margin:0;"><button type="submit" class="btn-danger">Limpar Tudo</button></form>
                                    {% endif %}
                                </td>
                            </tr>
                            {% for c in comps_fat %}
                            <tr style="background: #ffffff; font-size: 0.9em;">
                                <td style="padding-left: 25px; color: {% if c.comp == 'FANTASMA' %}#cc0000{% else %}#005a92{% endif %};">└─ <strong>{{ c.nome_exibicao }}</strong></td>
                                <td style="color: #666; font-style: italic;">Ativa</td>
                                <td>{{ c.linhas }}</td>
                                <td>
                                    <form action="/admin/limpar_competencia" method="post" onsubmit="return confirm('Apagar {{ c.nome_exibicao }}?');" style="margin:0;">
                                        <input type="hidden" name="comp" value="{{ c.comp }}">
                                        <button type="submit" class="btn-danger" style="background:#e67e22;">Excluir</button>
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
                                    <form action="/admin/limpar/{{ t_id }}" method="post" onsubmit="return confirm('Apagar a tabela {{ info.desc }}?');" style="margin:0;"><button type="submit" class="btn-danger">Limpar</button></form>
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
        document.getElementById('upload-btn').disabled = true;
        xhr.upload.addEventListener('progress', function(e) {
            if (e.lengthComputable) {
                const percent = Math.round((e.loaded / e.total) * 100);
                document.getElementById('progress-bar').style.width = percent + '%';
                if (percent < 100) { document.getElementById('progress-text').innerText = 'Enviando arquivos: ' + percent + '%'; } 
                else {
                    document.getElementById('progress-text').innerText = 'Upload 100%! O BD está processando em lotes de 200.000...';
                    document.getElementById('progress-bar').style.backgroundColor = 'var(--amarelo-postal)';
                }
            }
        });
        xhr.onload = function() {
            if (xhr.status === 200) { document.open(); document.write(xhr.responseText); document.close(); } 
            else { alert('Erro no servidor.'); window.location.reload(); }
        };
        xhr.open('POST', '/admin_upload', true);
        xhr.send(formData);
    });
</script>
</body>
</html>
"""

# =====================================================================
# AUXILIARES DE CÁLCULO FINANCEIRO
# =====================================================================
def aplicar_reajustes_simulados(df_cruzado, f):
    if df_cruzado.empty: return df_cruzado
        
    df = df_cruzado.copy()
    v_col = _find_column(df, [COL_VALOR_PAGO, 'VALOR_PAG', 'VALOR_PAGO', 'VALOR', 'VALORPAGOUNIT', 'VALORAPRESENTADOUNIT', 'VALOR_APRES'])
    if not v_col: return df

    df['VALOR_BASE'] = pd.to_numeric(df[v_col], errors='coerce').fillna(0)
    
    # O Extrator de Códigos Específicos (Arranca de Faixa/Dotação)
    codigos_excecao = [normalize_id_digits(x) for x in f['itens_exc'].split(';') if x.strip()]
    if not codigos_excecao: 
        codigos_excecao = [normalize_id_digits(x) for x in f['itens_exc'].split(',') if x.strip()]
        
    df['ORIGEM_CALCULO'] = df['ORIGEM_INICIAL']
    mask_exc = df['_COD_LIMPO_'].isin(codigos_excecao) & (len(codigos_excecao) > 0)
    df.loc[mask_exc, 'ORIGEM_CALCULO'] = 'Item Específico'

    df['TAXA_SOLICITADA'] = 0.0
    df['TAXA_CONCEDIDA'] = 0.0

    if f['modo_aplicacao'] == 'LINEAR':
        if f['sol_linear'] != 0.0 or f['conc_linear'] != 0.0:
            df['TAXA_SOLICITADA'] = f['sol_linear']
            df['TAXA_CONCEDIDA'] = f['conc_linear']
            df['ORIGEM_CALCULO'] = 'Modo Linear'
    else:
        for label, key in TIPOS_DESPESA:
            mask_tipo = df['TIPO_DESPESA_FINAL'] == label
            
            mask_dot = mask_tipo & (df['ORIGEM_CALCULO'] == 'Dotação')
            df.loc[mask_dot, 'TAXA_SOLICITADA'] = f.get(f'sol_dot_{key}', 0.0)
            df.loc[mask_dot, 'TAXA_CONCEDIDA'] = f.get(f'conc_dot_{key}', 0.0)
            
            mask_fai = mask_tipo & (df['ORIGEM_CALCULO'] == 'Faixa de Eventos')
            df.loc[mask_fai, 'TAXA_SOLICITADA'] = f.get(f'sol_fai_{key}', 0.0)
            df.loc[mask_fai, 'TAXA_CONCEDIDA'] = f.get(f'conc_fai_{key}', 0.0)

        if mask_exc.any():
            df.loc[mask_exc, 'TAXA_SOLICITADA'] = f['sol_exc']
            df.loc[mask_exc, 'TAXA_CONCEDIDA'] = f['conc_exc']

    df['VALOR_SOLICITADO'] = df['VALOR_BASE'] * (1 + (df['TAXA_SOLICITADA'] / 100))
    df['VALOR_CONCEDIDO'] = df['VALOR_BASE'] * (1 + (df['TAXA_CONCEDIDA'] / 100))
    df['CUSTO_EVITADO'] = df['VALOR_SOLICITADO'] - df['VALOR_CONCEDIDO']
    
    return df

def processa_filtros_request(req):
    f = {
        'step': req.args.get('step', ''),
        'modo_aplicacao': req.args.get('modo_aplicacao', 'POR_TIPO'),
        'ipca': float(req.args.get('ipca', '0.00') or 0.0),
        'analista': req.args.get('analista', '').strip(),
        'gestor': req.args.get('gestor', '').strip(),
        'tipo_neg': req.args.get('tipo_neg', 'TODOS').strip(),
        'uf_alvo': req.args.get('uf_alvo', '').strip(),
        'cnpj_alvo': req.args.get('cnpj_alvo', '').strip(),
        'busca_por': req.args.get('busca_por', 'CNPJ').strip(),
        'itens_exc': req.args.get('itens_exc', '').strip(),
        'sol_linear': float(req.args.get('sol_linear', '0.00') or 0.0),
        'conc_linear': float(req.args.get('conc_linear', '0.00') or 0.0),
        'sol_exc': float(req.args.get('sol_exc', '0.00') or 0.0),
        'conc_exc': float(req.args.get('conc_exc', '0.00') or 0.0)
    }
    f['comp_list'] = req.args.getlist('comp')
    
    for _, key in TIPOS_DESPESA:
        f[f'sol_dot_{key}'] = float(req.args.get(f'sol_dot_{key}', '0.00') or 0.0)
        f[f'conc_dot_{key}'] = float(req.args.get(f'conc_dot_{key}', '0.00') or 0.0)
        f[f'sol_fai_{key}'] = float(req.args.get(f'sol_fai_{key}', '0.00') or 0.0)
        f[f'conc_fai_{key}'] = float(req.args.get(f'conc_fai_{key}', '0.00') or 0.0)
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
    step = f['step']
    
    totais = {'faturamento_total': '0,00', 'total_solicitado': '0,00', 'linhas_faturamento': 0, 'total_concedido': '0,00', 'custo_evitado': '0,00'}
    itens_detalhe = []
    tem_dados = False
    comps_disponiveis = []
    bases_dict = {'Dotação': {}, 'Faixa de Eventos': {}, 'Item Específico': {}}

    if engine:
        try:
            with engine.connect() as conn:
                res = conn.execute(text("SELECT DISTINCT \"COMPETENCIA\" FROM faturamento ORDER BY \"COMPETENCIA\" DESC"))
                # Pula os fantasmas na tela principal
                comps_disponiveis = [r[0] for r in res if r[0] and r[0] not in ['None', 'NaN', 'SEM_COMPETENCIA', '']]
        except: pass
    
    if step in ['1', '2'] and f['comp_list'] and engine:
        try:
            # Farejador Dinâmico Blindado (em Lotes)
            df_fat = ler_e_filtrar_faturamento(engine, f['comp_list'], f)
            
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
                
                # Se for só o step 1 (Carregar), as taxas são zeradas. Se for step 2 (Calcular), usa as da tela
                f_calc = f.copy()
                if step == '1':
                    for k in f_calc:
                        if k.startswith('sol_') or k.startswith('conc_'): f_calc[k] = 0.0

                df_final = aplicar_reajustes_simulados(df_cruzado, f_calc)
                
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

                    # Renderiza os valores na aba
                    base_grp = df_final.groupby(['ORIGEM_CALCULO', 'TIPO_DESPESA_FINAL'])['VALOR_BASE'].sum().to_dict()
                    for (origem, tipo), valor in base_grp.items():
                        if origem not in bases_dict: bases_dict[origem] = {}
                        bases_dict[origem][tipo] = valor
                    
                    if 'Item Específico' in df_final['ORIGEM_CALCULO'].values:
                        bases_dict['Item Específico']['TOTAL'] = df_final[df_final['ORIGEM_CALCULO'] == 'Item Específico']['VALOR_BASE'].sum()

                    grupo = df_final.groupby(['TIPO_DESPESA_FINAL', 'ORIGEM_CALCULO']).agg(
                        qtd=('VALOR_BASE', 'count'), v_base=('VALOR_BASE', 'sum'),
                        v_sol=('VALOR_SOLICITADO', 'sum'), v_con=('VALOR_CONCEDIDO', 'sum')
                    ).reset_index()

                    for _, r in grupo.iterrows():
                        itens_detalhe.append({
                            'tipo_despesa': r['TIPO_DESPESA_FINAL'], 'origem': r['ORIGEM_CALCULO'], 'qtd': r['qtd'],
                            'valor_base': f"{r['v_base']:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                            'valor_sol': f"{r['v_sol']:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                            'valor_con': f"{r['v_con']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                        })
        except Exception as e:
            flash(f"Erro ao processar SQL: {str(e)}", "error")

    q_str = request.query_string.decode('utf-8')
    return render_template_string(HTML_DASHBOARD, step=step, totais=totais, itens_detalhe=itens_detalhe, periodo_base=", ".join(f['comp_list']) or "Nenhuma", filtros=f, tem_dados=tem_dados, comps_disponiveis=comps_disponiveis, tipos_despesa=TIPOS_DESPESA, bases_dict=bases_dict, query_string=q_str)

@app.route('/exportar')
def exportar():
    f = processa_filtros_request(request)
    if not f['comp_list'] or not engine: return redirect(url_for('dashboard'))
    try:
        df_fat = ler_e_filtrar_faturamento(engine, f['comp_list'], f)
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
        return send_file(output, download_name="Reajuste_Export.xlsx", as_attachment=True)
    except Exception: return redirect(url_for('dashboard'))

@app.route('/exportar_pdf')
def exportar_pdf():
    f = processa_filtros_request(request)
    if not f['comp_list'] or not engine: return redirect(url_for('dashboard'))
    try:
        df_fat = ler_e_filtrar_faturamento(engine, f['comp_list'], f)
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
            'comp_list': f['comp_list'], 'uf_alvo': f['uf_alvo'], 'cnpj_alvo': f['cnpj_alvo'],
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
        response.headers.set('Content-Disposition', 'attachment; filename="Relatorio_Impacto.pdf"')
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
                    for row in res: 
                        c_name = row[0]
                        if not c_name or c_name in ['None', 'NaN', 'SEM_COMPETENCIA', '']:
                            comps_fat.append({'comp': 'FANTASMA', 'nome_exibicao': 'FANTASMAS (S/ Data)', 'linhas': row[1]})
                        else:
                            comps_fat.append({'comp': c_name, 'nome_exibicao': c_name, 'linhas': row[1]})
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

@app.route('/admin/limpar_competencia', methods=['POST'])
def limpar_competencia():
    comp = request.form.get('comp')
    if engine:
        try:
            with engine.begin() as conn: 
                if comp == 'FANTASMA':
                    conn.execute(text("DELETE FROM faturamento WHERE \"COMPETENCIA\" IS NULL OR \"COMPETENCIA\" IN ('', 'None', 'NaN', 'SEM_COMPETENCIA')"))
                    flash("Arquivos fantasmas excluídos com sucesso!", "success")
                else:
                    conn.execute(text(f"DELETE FROM faturamento WHERE \"COMPETENCIA\" = '{comp}'"))
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
