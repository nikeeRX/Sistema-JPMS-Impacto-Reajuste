import os
import re
import traceback
import unicodedata
import pandas as pd
import numpy as np
from flask import Flask, request, render_template_string, redirect, url_for, flash, send_from_directory
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()

# =====================================================================
# CONSTANTES GLOBAIS
# =====================================================================
COL_EVENTO = 'EVENTO'
COL_VALOR_PAGO = 'VALOR_PAG'

# =====================================================================
# FUNÇÕES DE PROCESSAMENTO E CRUZAMENTO
# =====================================================================
def _remover_acentos(txt):
    if not txt: return ""
    return "".join(c for c in unicodedata.normalize("NFD", str(txt)) if unicodedata.category(c) != "Mn").upper()

def _limpar_texto_chave(txt):
    s = str(txt).strip().upper()
    s = _remover_acentos(s)
    return re.sub(r"\s+", " ", s)

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

def cruzar_bases(df_fat, df_mat, df_die, df_dot, df_fai, df_pre, is_regional=False, uf_regional=None, itens_excecao=None):
    try:
        df = df_fat.copy()
        ev_col = _find_column(df, [COL_EVENTO, "EVENTO", "COD_EVENTO", "ESTRUTURA", "CODIGO"])
        ds_col = _find_column(df, ["DESCRICAO_EVENTO", "DESCRICAO", "EVENTO_DESC", "DESCRICAOEVENTO"])
        grau_col = _find_column(df, ["DESCRICAO_GRAU", "GRAU"])
        
        df["DESCRICAO_EVENTO"] = df[ds_col].fillna("").astype(str) if ds_col else ""
        df["_COD_LIMPO_"] = df[ev_col].fillna("").astype(str).apply(normalize_id_digits) if ev_col else ""
        
        if itens_excecao:
            ex_l = [str(x).strip() for x in itens_excecao]
            df["ORIGEM"] = np.where(df["_COD_LIMPO_"].isin(ex_l), "Item Específico", "Pendente")
        else: df["ORIGEM"] = "Pendente"

        mask = (df["ORIGEM"] == "Pendente")
        if mask.any() and df_dot is not None and not df_dot.empty:
            df_d = df_dot.copy()
            d_ev = _find_column(df_d, ["ESTRUTURA", "CODIGO", "EVENTO"])
            d_ds = _find_column(df_d, ["DESC_EVENTO", "DESCRICAO"])
            if is_regional and uf_regional and uf_regional != "(todas)":
                uf_c = _find_column(df_d, ["UF", "ESTADO"])
                if uf_c: df_d = df_d[df_d[uf_c].astype(str).str.upper() == uf_regional.upper()]
            
            df["CHAVE"] = df["_COD_LIMPO_"] + "-" + df["DESCRICAO_EVENTO"].apply(_limpar_texto_chave)
            df_d["CHAVE_DOT"] = df_d[d_ev].fillna("").astype(str).apply(normalize_id_digits) + "-" + df_d[d_ds].fillna("").astype(str).apply(_limpar_texto_chave)
            dot_keys = set(df_d["CHAVE_DOT"].unique())
            df.loc[mask, "ORIGEM"] = np.where(df.loc[mask, "CHAVE"].isin(dot_keys), "Dotação", "Faixa de Evento")
        else: df.loc[mask, "ORIGEM"] = "Faixa de Evento"

        def get_ref_codes(rdf, is_dieta=False):
            if rdf is None or rdf.empty: return set()
            c = _find_column(rdf, ["ESTRUTURA", "CODIGO", "EVENTOS"]) if is_dieta else _find_column(rdf, ["EVENTOS", "EVENTO", "ESTRUTURA"])
            return set(rdf[c or rdf.columns[0]].fillna("").astype(str).apply(normalize_id_digits).unique())

        s_perf, s_diet = get_ref_codes(df_mat), get_ref_codes(df_die, True)
        t_col = _find_column(df, ["TIPO_DESPESA_FINAL", "TIPODESPESA", "TIPO", "TIPO_DESPESA"])
        df["TIPO_DESPESA_FINAL"] = df[t_col].fillna("OUTROS").astype(str).str.upper() if t_col else "OUTROS"
        
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
# CONFIGURAÇÃO DO FLASK E BANCO DE DADOS
# =====================================================================
app = Flask(__name__)
app.secret_key = "chave_secreta_super_segura_gered"

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL) if DATABASE_URL else None

# =====================================================================
# CSS E HTML EMBUTIDOS (VISUAL POSTAL SAÚDE)
# =====================================================================
CSS_PADRAO = """
<style>
    :root { 
        --azul-postal: #002c52; 
        --azul-claro: #005a92; 
        --amarelo-postal: #f9b200; 
        --verde-ok: #007a33; 
        --vermelho-alerta: #cc0000; 
        --fundo: #eef2f5; 
    }
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--fundo); color: #333; margin: 0; }
    
    .header { 
        background-color: var(--azul-postal); 
        color: white; 
        padding: 15px 40px; 
        display: flex; 
        justify-content: space-between; 
        align-items: center; 
        border-bottom: 5px solid var(--amarelo-postal); 
    }
    .header-logo-container { display: flex; align-items: center; gap: 20px; }
    .logo-img { 
        height: 55px; 
        background: white; 
        padding: 8px 15px; 
        border-radius: 6px; 
        box-shadow: 0 2px 4px rgba(0,0,0,0.2); 
    }
    
    .container { padding: 20px 40px; }
    .card { 
        background: white; 
        padding: 25px; 
        border-radius: 8px; 
        box-shadow: 0 4px 6px rgba(0,0,0,0.05); 
        margin-bottom: 20px; 
        border-top: 4px solid var(--azul-claro); 
    }
    
    .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; }
    .metric-box { padding: 15px; border: 1px solid #eee; border-radius: 6px; text-align: center; }
    .metric-box h4 { margin: 0 0 10px 0; color: #666; font-size: 0.9em; text-transform: uppercase; }
    .metric-box .valor { font-size: 1.5em; font-weight: bold; color: var(--azul-postal); margin-bottom: 5px; }
    .metric-box .sub { font-size: 0.85em; color: #888; }
    
    .impacto-card { background-color: #fff9e6; border-top: 4px solid var(--amarelo-postal); }
    .impacto-card .valor { color: var(--azul-postal); }
    
    table { width: 100%; border-collapse: collapse; margin-top: 15px; }
    th { background-color: var(--azul-postal); color: white; padding: 12px; text-align: left; border-bottom: 3px solid var(--amarelo-postal); }
    td { padding: 10px 12px; border-bottom: 1px solid #eee; }
    tr:hover { background-color: #f4f7f6; }
    
    .btn { background-color: var(--amarelo-postal); color: var(--azul-postal); font-weight: bold; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; transition: background-color 0.3s; }
    .btn:hover { background-color: #e0a100; }
    .btn-success { background-color: var(--azul-claro); color: white; font-weight: normal; }
    .btn-success:hover { background-color: var(--azul-postal); }
    
    .form-group { margin-bottom: 15px; }
    .form-group label { display: block; margin-bottom: 5px; font-weight: bold; color: var(--azul-postal); }
    .alert { padding: 15px; border-radius: 4px; margin-bottom: 20px; }
    .alert-success { background: #e6f4ea; color: var(--verde-ok); border: 1px solid var(--verde-ok); }
    .alert-info { background: #e3f2fd; color: var(--azul-postal); border: 1px solid var(--azul-claro); }
</style>
"""

HTML_DASHBOARD = CSS_PADRAO + """
<div class="header">
    <div class="header-logo-container">
        <img src="/Logo_Postal-03.png" class="logo-img" alt="Postal Saúde">
        <div>
            <h2 style="margin:0;">GERED - Sistema de Impacto de Reajuste</h2>
            <p style="margin:5px 0 0 0; color: #d4e3ef;">Prestador: {{ prestador_nome }} | Competência: {{ periodo_base }}</p>
        </div>
    </div>
    <a href="/admin" class="btn">Painel Admin (Uploads)</a>
</div>

<div class="container">
    {% with messages = get_flashed_messages(category_filter=["info"]) %}
      {% if messages %}
        {% for message in messages %}
          <div class="alert alert-info"><strong>Aviso:</strong> {{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div class="card" style="background-color: var(--azul-postal); color: white; border-top: none;">
        <h3 style="margin-top:0; color: var(--amarelo-postal);">Painel de Controle: Análise de Impacto</h3>
        <form action="/analisar" method="post" style="display: flex; gap: 15px; align-items: flex-end;">
            <div class="form-group" style="margin-bottom: 0; flex: 1;">
                <label style="color: white;">Competência do Faturamento (Ex: 2026-04):</label>
                <input type="text" name="competencia_alvo" style="width: 100%; padding: 10px; border-radius: 4px; border: none; box-sizing: border-box;" placeholder="YYYY-MM" required>
            </div>
            <button type="submit" class="btn" style="height: 38px;">Cruzar Bases e Calcular Impacto</button>
        </form>
    </div>

    <div class="card">
        <h3 style="margin-top:0; color: var(--azul-postal);">Resumo Financeiro Final</h3>
        <div class="grid-4">
            <div class="metric-box">
                <h4>Faturamento Total</h4>
                <div class="valor">R$ {{ totais.faturamento_total }}</div>
            </div>
            <div class="metric-box">
                <h4>Total Solicitado</h4>
                <div class="valor" style="color: var(--vermelho-alerta);">R$ {{ totais.total_solicitado }}</div>
                <div class="sub">{{ totais.pct_solicitado }}% do faturamento</div>
            </div>
            <div class="metric-box">
                <h4>Total Concedido</h4>
                <div class="valor" style="color: var(--verde-ok);">R$ {{ totais.total_concedido }}</div>
                <div class="sub">{{ totais.pct_concedido }}% do faturamento</div>
            </div>
            <div class="metric-box impacto-card">
                <h4>Custo Evitado</h4>
                <div class="valor">R$ {{ totais.custo_evitado }}</div>
            </div>
        </div>
    </div>

    <div class="card">
        <h3 style="margin-top:0; color: var(--azul-postal);">Detalhamento por Item de Configuração</h3>
        <table>
            <thead>
                <tr>
                    <th>Item / Serviço</th>
                    <th>Valor Base (R$)</th>
                    <th>Impacto Solicitado (R$)</th>
                    <th>Impacto Concedido (R$)</th>
                </tr>
            </thead>
            <tbody>
                {% for item in itens_detalhe %}
                <tr>
                    <td><strong>{{ item.descricao }}</strong></td>
                    <td>R$ {{ item.valor_base }}</td>
                    <td style="color: var(--vermelho-alerta); font-weight: bold;">R$ {{ item.delta_solicitado }}</td>
                    <td style="color: var(--verde-ok); font-weight: bold;">R$ {{ item.delta_concedido }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
"""

HTML_ADMIN = CSS_PADRAO + """
<div class="header">
    <div class="header-logo-container">
        <img src="/Logo_Postal-03.png" class="logo-img" alt="Postal Saúde">
        <div>
            <h2 style="margin:0;">Administração de Banco de Dados</h2>
        </div>
    </div>
    <a href="/" class="btn">Voltar ao Dashboard</a>
</div>
<div class="container">
    <div class="card" style="max-width: 600px; margin: 0 auto; border-top: 4px solid var(--amarelo-postal);">
        <h3 style="margin-top:0; color: var(--azul-postal);">Upload de Bases (Parquet / CSV / TXT / Excel)</h3>
        {% with messages = get_flashed_messages(category_filter=["success"]) %}
          {% if messages %}
            {% for message in messages %}
              <div class="alert alert-success">{{ message }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}
        {% with messages = get_flashed_messages(category_filter=["error"]) %}
          {% if messages %}
            {% for message in messages %}
              <div class="alert" style="background: #fde8e8; color: var(--vermelho-alerta); border: 1px solid var(--vermelho-alerta);">{{ message }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}
        
        <form action="/admin_upload" method="post" enctype="multipart/form-data">
            <div class="form-group">
                <label>Selecione a Base de Destino:</label>
                <select name="tipo_base" style="width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px;" required>
                    <option value="faturamento">Faturamento (Mês a Mês)</option>
                    <option value="prestadores">Prestadores</option>
                    <option value="materiais">Materiais Perfurocortantes</option>
                    <option value="dietas">Dietas</option>
                    <option value="dotacoes">Dotações</option>
                    <option value="faixas">Faixa de Eventos</option>
                </select>
            </div>
            
            <div class="form-group">
                <label>Competência (Ex: 2026-04) - Apenas para Faturamento:</label>
                <input type="text" name="competencia" style="width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px;" placeholder="YYYY-MM">
            </div>

            <div class="form-group">
                <label>Arquivo (.parquet, .csv, .txt, .xlsx):</label>
                <input type="file" name="arquivo" style="width: 100%; padding: 10px; border: 1px dashed var(--azul-claro); border-radius: 4px;" required>
            </div>
            
            <button type="submit" class="btn btn-success" style="width: 100%; margin-top: 10px; font-size: 16px;">Processar e Enviar para o Banco</button>
        </form>
    </div>
</div>
"""

# =====================================================================
# ROTAS DA APLICAÇÃO
# =====================================================================

@app.route('/Logo_Postal-03.png')
def serve_logo():
    return send_from_directory(os.getcwd(), 'Logo_Postal-03.png')

@app.route('/')
def dashboard():
    dados_mock = {
        'prestador_nome': 'Hospital Geral Misto (Dados de Teste)',
        'periodo_base': 'Aguardando Análise',
        'totais': {
            'faturamento_total': '277.173,22',
            'total_solicitado': '11.708,58',
            'pct_solicitado': '4.83',
            'total_concedido': '6.131,70',
            'pct_concedido': '0.72',
            'custo_evitado': '5.576,88'
        },
        'itens_detalhe': [
            {'descricao': '10101012 - CONSULTA EM PRONTO SOCORRO', 'valor_base': '45.000,00', 'delta_solicitado': '2.250,00', 'delta_concedido': '1.100,00'},
            {'descricao': '40805018 - RX DE TORAX PA', 'valor_base': '0,00', 'delta_solicitado': '0,00', 'delta_concedido': '0,00'}, 
            {'descricao': 'DIETAS (Consolidado)', 'valor_base': '12.450,00', 'delta_solicitado': '622,50', 'delta_concedido': '0,00'}
        ]
    }
    return render_template_string(HTML_DASHBOARD, **dados_mock)

@app.route('/analisar', methods=['POST'])
def analisar():
    competencia_alvo = request.form.get('competencia_alvo')
    flash(f"Processamento solicitado para a competência {competencia_alvo}. O motor de cruzamento SQL será ligado no próximo passo!", "info")
    return redirect(url_for('dashboard'))

@app.route('/admin')
def admin():
    return render_template_string(HTML_ADMIN)

@app.route('/admin_upload', methods=['POST'])
def admin_upload():
    if 'arquivo' not in request.files:
        flash("Nenhum arquivo enviado!", "error")
        return redirect(url_for('admin'))
        
    arquivo = request.files['arquivo']
    tipo_base = request.form.get('tipo_base')
    competencia = request.form.get('competencia')
    
    if arquivo.filename == '':
        flash("Nenhum arquivo selecionado!", "error")
        return redirect(url_for('admin'))

    if not engine:
        flash("Erro crítico: Banco de dados não conectado!", "error")
        return redirect(url_for('admin'))

    try:
        if arquivo.filename.endswith('.parquet'):
            df = pd.read_parquet(arquivo)
        elif arquivo.filename.endswith('.csv') or arquivo.filename.endswith('.txt'):
            # LEITOR SUPORTANDO CSV E TXT COM SUPORTE A ENCODINGS CORPORATIVOS
            try:
                df = pd.read_csv(arquivo, sep=None, engine='python', encoding='utf-8')
            except UnicodeDecodeError:
                arquivo.seek(0)
                df = pd.read_csv(arquivo, sep=None, engine='python', encoding='iso-8859-1')
        elif arquivo.filename.endswith('.xlsx'):
            df = pd.read_excel(arquivo)
        else:
            flash("Formato não suportado. Use Parquet, CSV, TXT ou Excel.", "error")
            return redirect(url_for('admin'))

        # Lógica para garantir colunas críticas de localização
        for col in ['UF', 'AP']:
            if col not in df.columns:
                df[col] = None
        
        # Lógica de agrupamento financeiro de desconto na primeira linha
        if 'VLR_DESCONTO_OBTIDO' in df.columns:
            df['VLR_DESCONTO_OBTIDO'] = pd.to_numeric(df['VLR_DESCONTO_OBTIDO'], errors='coerce').fillna(0)
            total_desconto = df['VLR_DESCONTO_OBTIDO'].sum()
            df['VLR_DESCONTO_OBTIDO'] = 0.0
            if not df.empty:
                df.at[df.index[0], 'VLR_DESCONTO_OBTIDO'] = total_desconto

        with engine.begin() as conn:
            if tipo_base == 'faturamento':
                if competencia:
                    df['COMPETENCIA'] = competencia
                df.to_sql('faturamento', con=conn, if_exists='append', index=False)
                flash(f"Sucesso! {len(df)} linhas de Faturamento ({competencia}) inseridas no banco.", "success")
            else:
                df.to_sql(tipo_base, con=conn, if_exists='replace', index=False)
                flash(f"Sucesso! Base de {tipo_base} atualizada com {len(df)} linhas.", "success")

    except Exception as e:
        flash(f"Erro ao processar arquivo: {str(e)}", "error")

    return redirect(url_for('admin'))
