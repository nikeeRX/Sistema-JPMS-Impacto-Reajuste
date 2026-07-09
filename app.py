import os
import re
import traceback
import unicodedata
import pandas as pd
import numpy as np
import io
from flask import Flask, request, render_template_string, redirect, url_for, flash, send_from_directory, send_file
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

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

def cruzar_bases(df_fat, df_mat, df_die, df_dot, df_fai, df_pre, is_regional=False, uf_regional=None, itens_excecao=None):
    try:
        df = df_fat.copy()
        
        for col in ['UF', 'AP', 'CNPJ']:
            if col not in df.columns:
                df[col] = None

        ev_col = _find_column(df, [COL_EVENTO, "EVENTO", "COD_EVENTO", "ESTRUTURA", "CODIGO"])
        ds_col = _find_column(df, ["DESCRICAO_EVENTO", "DESCRICAO", "EVENTO_DESC", "DESCRICAOEVENTO"])
        grau_col = _find_column(df, ["DESCRICAO_GRAU", "GRAU"])
        
        df["DESCRICAO_EVENTO"] = df[ds_col].fillna("").astype(str) if ds_col else ""
        df["_COD_LIMPO_"] = df[ev_col].fillna("").astype(str).apply(normalize_id_digits) if ev_col else ""
        
        df["ORIGEM"] = "Pendente"
        mask = (df["ORIGEM"] == "Pendente")
        
        if mask.any() and df_dot is not None and not df_dot.empty:
            df_d = df_dot.copy()
            d_ev = _find_column(df_d, ["ESTRUTURA", "CODIGO", "EVENTO"])
            d_ds = _find_column(df_d, ["DESC_EVENTO", "DESCRICAO"])
            
            df["CHAVE"] = df["_COD_LIMPO_"] + "-" + df["DESCRICAO_EVENTO"].apply(_limpar_texto_chave)
            df_d["CHAVE_DOT"] = df_d[d_ev].fillna("").astype(str).apply(normalize_id_digits) + "-" + df_d[d_ds].fillna("").astype(str).apply(_limpar_texto_chave)
            dot_keys = set(df_d["CHAVE_DOT"].unique())
            df.loc[mask, "ORIGEM"] = np.where(df.loc[mask, "CHAVE"].isin(dot_keys), "Dotação", "Faixa de Evento")
        else: 
            df.loc[mask, "ORIGEM"] = "Faixa de Evento"

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
# CSS E HTML EMBUTIDOS (DESIGN IDENTIDADE VISUAL POSTAL SAÚDE)
# =====================================================================
CSS_PADRAO = """
<style>
    :root { --azul-postal: #002c52; --azul-claro: #005a92; --amarelo-postal: #f9b200; --verde-ok: #007a33; --vermelho-alerta: #cc0000; --fundo: #eef2f5; }
    body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: var(--fundo); color: #333; margin: 0; }
    .header { background-color: var(--azul-postal); color: white; padding: 15px 40px; display: flex; justify-content: space-between; align-items: center; border-bottom: 5px solid var(--amarelo-postal); }
    .logo-img { height: 55px; background: white; padding: 8px 15px; border-radius: 6px; box-shadow: 0 2px 4px rgba(0,0,0,0.2); }
    .container { padding: 20px 40px; }
    .card { background: white; padding: 25px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); margin-bottom: 20px; border-top: 4px solid var(--azul-claro); }
    .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; }
    .form-group { margin-bottom: 15px; }
    .form-group label { display: block; margin-bottom: 5px; font-weight: bold; color: var(--azul-postal); font-size: 0.9em; }
    .form-control { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; font-size: 1em; }
    .metric-box { padding: 15px; border: 1px solid #eee; border-radius: 6px; text-align: center; background: #fafafa; }
    .metric-box h4 { margin: 0 0 10px 0; color: #666; font-size: 0.85em; text-transform: uppercase; }
    .metric-box .valor { font-size: 1.6em; font-weight: bold; color: var(--azul-postal); margin-bottom: 5px; }
    .metric-box .sub { font-size: 0.85em; color: #888; }
    .impacto-card { background-color: #fff9e6; border-top: 4px solid var(--amarelo-postal); }
    table { width: 100%; border-collapse: collapse; margin-top: 15px; }
    th { background-color: var(--azul-postal); color: white; padding: 12px; text-align: left; border-bottom: 3px solid var(--amarelo-postal); }
    td { padding: 10px 12px; border-bottom: 1px solid #eee; }
    tr:hover { background-color: #f4f7f6; }
    .btn { background-color: var(--amarelo-postal); color: var(--azul-postal); font-weight: bold; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; text-align: center; }
    .btn:hover { background-color: #e0a100; }
    .btn-success { background-color: var(--verde-ok); color: white; }
    .btn-success:hover { background-color: #004d20; }
    .btn-danger { background-color: var(--vermelho-alerta); color: white; border: none; padding: 5px 10px; border-radius: 4px; cursor: pointer; }
    .btn-danger:hover { background-color: #990000; }
    .alert { padding: 15px; border-radius: 4px; margin-bottom: 20px; }
    .alert-success { background: #e6f4ea; color: var(--verde-ok); border: 1px solid var(--verde-ok); }
    .alert-danger { background: #fde8e8; color: var(--vermelho-alerta); border: 1px solid var(--vermelho-alerta); }
    
    .progress-wrapper { background: #eef2f5; border-radius: 4px; overflow: hidden; height: 25px; margin-top: 20px; border: 1px solid #ccc; display: none; }
    .progress-bar { background: var(--verde-ok); height: 100%; width: 0%; transition: width 0.3s ease, background-color 0.5s ease; }
    .progress-text { margin-top: 8px; font-size: 0.95em; color: var(--azul-postal); font-weight: bold; display: none; text-align: center; }
</style>
"""

HTML_DASHBOARD = CSS_PADRAO + """
<div class="header">
    <div style="display: flex; align-items: center; gap: 20px;">
        <img src="/Logo_Postal-03.png" class="logo-img">
        <div>
            <h2 style="margin:0;">GERED - Sistema de Impacto de Reajuste</h2>
            <p style="margin:5px 0 0 0; color: #d4e3ef;">Competência Selecionada: <strong>{{ periodo_base }}</strong></p>
        </div>
    </div>
    <a href="/admin" class="btn">Painel Admin (Uploads / Gerenciador)</a>
</div>

<div class="container">
    {% with messages = get_flashed_messages(category_filter=["error"]) %}{% if messages %}{% for m in messages %}<div class="alert alert-danger">{{ m }}</div>{% endfor %}{% endif %}{% endwith %}

    <div class="card" style="border-top: 4px solid var(--azul-postal);">
        <h3 style="margin-top:0; color: var(--azul-postal);">Módulo de Configuração de Análise e Taxas</h3>
        <form action="/" method="get">
            <div class="grid-4">
                <div class="form-group">
                    <label>Competência Alvo (Banco):</label>
                    <input type="text" name="comp" class="form-control" value="{{ filtros.comp }}" placeholder="Digite o nome do arquivo ex: 092024" required>
                </div>
                <div class="form-group">
                    <label>Tipo de Negociação:</label>
                    <select name="tipo_neg" class="form-control">
                        <option value="TODOS" {% if filtros.tipo_neg == 'TODOS' %}selected{% endif %}>Todos os Itens (Geral)</option>
                        <option value="ESTADO" {% if filtros.tipo_neg == 'ESTADO' %}selected{% endif %}>Regional (Por Estado/UF)</option>
                        <option value="DIFERENCIADO" {% if filtros.tipo_neg == 'DIFERENCIADO' %}selected{% endif %}>Diferenciado (Por CNPJ)</option>
                        <option value="MISTO" {% if filtros.tipo_neg == 'MISTO' %}selected{% endif %}>Misto (Estado + CNPJ)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>UF Específica (Se Regional/Misto):</label>
                    <input type="text" name="uf_alvo" class="form-control" value="{{ filtros.uf_alvo }}" placeholder="Ex: GO, RS, PR">
                </div>
                <div class="form-group">
                    <label>CNPJ / Grupo (Se Diferenciado/Misto):</label>
                    <input type="text" name="cnpj_alvo" class="form-control" value="{{ filtros.cnpj_alvo }}" placeholder="Digite o CNPJ">
                </div>
            </div>

            <hr style="border:0; border-top:1px solid #ccc; margin: 20px 0;">
            <h4 style="margin: 0 0 15px 0; color: var(--azul-claro);">Definição das Propostas por Tipo de Despesa (%)</h4>
            
            <div class="grid-4">
                <div class="form-group"><label>Dietas (%):</label><input type="number" step="0.01" name="p_dietas" class="form-control" value="{{ filtros.p_dietas }}"></div>
                <div class="form-group"><label>Perfurocortantes (%):</label><input type="number" step="0.01" name="p_perfuro" class="form-control" value="{{ filtros.p_perfuro }}"></div>
                <div class="form-group"><label>Anestesista (%):</label><input type="number" step="0.01" name="p_anest" class="form-control" value="{{ filtros.p_anest }}"></div>
                <div class="form-group"><label>Materiais (%):</label><input type="number" step="0.01" name="p_mat" class="form-control" value="{{ filtros.p_mat }}"></div>
                
                <div class="form-group"><label>Medicamentos (%):</label><input type="number" step="0.01" name="p_med" class="form-control" value="{{ filtros.p_med }}"></div>
                <div class="form-group"><label>Diárias (%):</label><input type="number" step="0.01" name="p_dia" class="form-control" value="{{ filtros.p_dia }}"></div>
                <div class="form-group"><label>Taxas (%):</label><input type="number" step="0.01" name="p_taxa" class="form-control" value="{{ filtros.p_taxa }}"></div>
                <div class="form-group"><label>Gases (%):</label><input type="number" step="0.01" name="p_gas" class="form-control" value="{{ filtros.p_gas }}"></div>
                
                <div class="form-group"><label>OPME (%):</label><input type="number" step="0.01" name="p_opme" class="form-control" value="{{ filtros.p_opme }}"></div>
                <div class="form-group"><label>SADT / Exames (%):</label><input type="number" step="0.01" name="p_sadt" class="form-control" value="{{ filtros.p_sadt }}"></div>
                <div class="form-group"><label>Honorários (%):</label><input type="number" step="0.01" name="p_hon" class="form-control" value="{{ filtros.p_hon }}"></div>
                <div class="form-group"><label style="color:var(--amarelo-postal);">Outros / Geral (%):</label><input type="number" step="0.01" name="p_outros" class="form-control" value="{{ filtros.p_outros }}"></div>
            </div>
            
            <div style="text-align: right; margin-top: 10px;">
                <button type="submit" class="btn" style="width: 250px; font-size:1.05em;">Calcular Impacto Real</button>
            </div>
        </form>
    </div>

    <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h3 style="margin:0; color: var(--azul-postal);">Resumo Financeiro Consolidado da Proposta</h3>
            {% if tem_dados %}
            <a href="/exportar?comp={{ filtros.comp }}&tipo_neg={{ filtros.tipo_neg }}&uf_alvo={{ filtros.uf_alvo }}&cnpj_alvo={{ filtros.cnpj_alvo }}&p_dietas={{ filtros.p_dietas }}&p_perfuro={{ filtros.p_perfuro }}&p_anest={{ filtros.p_anest }}&p_mat={{ filtros.p_mat }}&p_med={{ filtros.p_med }}&p_dia={{ filtros.p_dia }}&p_taxa={{ filtros.p_taxa }}&p_gas={{ filtros.p_gas }}&p_opme={{ filtros.p_opme }}&p_sadt={{ filtros.p_sadt }}&p_hon={{ filtros.p_hon }}&p_outros={{ filtros.p_outros }}" class="btn btn-success">📥 Baixar Planilha Final (Excel)</a>
            {% endif %}
        </div>
        
        <div class="grid-4" style="margin-top: 15px;">
            <div class="metric-box">
                <h4>Faturamento Total Lido</h4>
                <div class="valor">R$ {{ totais.faturamento_total }}</div>
                <div class="sub">{{ totais.linhas_faturamento }} linhas aplicadas</div>
            </div>
            <div class="metric-box">
                <h4>Impacto Solicitado (Base)</h4>
                <div class="valor" style="color: var(--azul-claro);">R$ {{ totais.faturamento_total }}</div>
                <div class="sub">Valor original faturado</div>
            </div>
            <div class="metric-box">
                <h4>Proposta Concedida</h4>
                <div class="valor" style="color: var(--verde-ok);">R$ {{ totais.total_concedido }}</div>
                <div class="sub">Com reajustes agregados</div>
            </div>
            <div class="metric-box impacto-card">
                <h4>Custo Evitado (Economia)</h4>
                <div class="valor" style="color: var(--vermelho-alerta);">R$ {{ totais.custo_evitado }}</div>
                <div class="sub">Diferença salva na negociação</div>
            </div>
        </div>
    </div>

    <div class="card">
        <h3 style="margin-top:0; color: var(--azul-postal);">Detalhamento de Impacto por Classificação e Regra</h3>
        <table>
            <thead>
                <tr>
                    <th>Grupo de Despesa Real</th>
                    <th>Origem da Regra</th>
                    <th>Qtd Itens</th>
                    <th>Valor Solicitado (R$)</th>
                    <th>Valor Concedido (R$)</th>
                </tr>
            </thead>
            <tbody>
                {% for item in itens_detalhe %}
                <tr>
                    <td><strong>{{ item.tipo_despesa }}</strong></td>
                    <td><span style="background: #eef2f5; padding: 4px 8px; border-radius: 4px; font-size:0.9em;">{{ item.origem }}</span></td>
                    <td>{{ item.qtd }}</td>
                    <td>R$ {{ item.valor_sol }}</td>
                    <td style="color: var(--verde-ok); font-weight: bold;">R$ {{ item.valor_con }}</td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="5" style="text-align: center; color: #888; padding: 30px;">Aguardando filtros. Digite a competência acima e execute o processamento.</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
"""

HTML_ADMIN = CSS_PADRAO + """
<div class="header">
    <div style="display: flex; align-items: center; gap: 20px;">
        <img src="/Logo_Postal-03.png" class="logo-img">
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
                    <small style="color: #666;">Se não preencher, o sistema usará o nome do arquivo (ex: '092024.parquet' vira '092024')</small>
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
                
                <div id="progress-wrapper" class="progress-wrapper">
                    <div id="progress-bar" class="progress-bar"></div>
                </div>
                <div id="progress-text" class="progress-text">Iniciando upload... 0%</div>
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
                                <td><strong>Faturamento Geral</strong><br><span style="font-family:monospace; font-size:0.8em; color:#777;">{{ t_id }}</span></td>
                                <td style="color:{% if info.linhas > 0 %}var(--verde-ok){% else %}#999{% endif %}; font-weight:bold;">{{ info.status }}</td>
                                <td><strong>{{ info.linhas }}</strong></td>
                                <td>
                                    {% if info.linhas > 0 %}
                                    <form action="/admin/limpar/{{ t_id }}" method="post" onsubmit="return confirm('Deseja apagar TODO o faturamento de todos os meses?');" style="margin:0;"><button type="submit" class="btn-danger" style="padding: 3px 8px; font-size: 0.85em;">Limpar Tudo</button></form>
                                    {% endif %}
                                </td>
                            </tr>
                            
                            {% for c in comps_fat %}
                            <tr style="background: #ffffff; font-size: 0.9em;">
                                <td style="padding-left: 25px; color: var(--azul-claro);">└─ Mês Salvo: <strong>{{ c.comp }}</strong></td>
                                <td style="color: #666; font-style: italic;">Competência Ativa</td>
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
                                <td><strong>{{ info.desc }}</strong><br><span style="font-family:monospace; font-size:0.8em; color:#777;">{{ t_id }}</span></td>
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
                
                if (percent < 100) {
                    document.getElementById('progress-text').innerText = 'Enviando arquivos pela rede: ' + percent + '%';
                } else {
                    document.getElementById('progress-text').innerText = 'Upload 100% concluído! O Banco de Dados está processando em lotes de 200.000 (Aguarde alguns minutos)...';
                    document.getElementById('progress-bar').style.backgroundColor = 'var(--amarelo-postal)';
                }
            }
        });

        xhr.onload = function() {
            if (xhr.status === 200) {
                document.open();
                document.write(xhr.responseText);
                document.close();
            } else {
                alert('Erro na resposta do servidor. A internet oscilou ou houve falha no processamento.');
                window.location.reload();
            }
        };

        xhr.open('POST', '/admin_upload', true);
        xhr.send(formData);
    });
</script>
"""

# =====================================================================
# AUXILIARES DE CÁLCULO FINANCEIRO (MÓDULO DE REAJUSTE)
# =====================================================================
def aplicar_reajustes_simulados(df_cruzado, f):
    if df_cruzado.empty:
        return df_cruzado
        
    df = df_cruzado.copy()
    v_col = _find_column(df, [COL_VALOR_PAGO, 'VALOR_PAG', 'VALOR_PAGO', 'VALOR'])
    if not v_col:
        return df

    # Filtros de Abrangência de Negociação
    if f['tipo_neg'] == 'ESTADO' and f['uf_alvo']:
        df = df[df['UF'].fillna('').astype(str).str.upper() == f['uf_alvo'].upper()]
    elif f['tipo_neg'] == 'DIFERENCIADO' and f['cnpj_alvo']:
        df = df[df['CNPJ'].fillna('').astype(str).str.contains(f['cnpj_alvo'], na=False)]
    elif f['tipo_neg'] == 'MISTO':
        if f['uf_alvo']:
            df = df[df['UF'].fillna('').astype(str).str.upper() == f['uf_alvo'].upper()]
        if f['cnpj_alvo']:
            df = df[df['CNPJ'].fillna('').astype(str).str.contains(f['cnpj_alvo'], na=False)]

    df['VALOR_SOLICITADO'] = pd.to_numeric(df[v_col], errors='coerce').fillna(0)
    df['VALOR_CONCEDIDO'] = df['VALOR_SOLICITADO'].copy()

    taxas_conhecidas = {
        'DIETAS': f['p_dietas'],
        'PERFUROCORTANTES': f['p_perfuro'],
        'ANESTESISTA': f['p_anest'],
        'MATERIAIS': f['p_mat'],
        'MEDICAMENTOS': f['p_med'],
        'DIARIAS': f['p_dia'],
        'TAXAS': f['p_taxa'],
        'GASES': f['p_gas'],
        'OPME': f['p_opme'],
        'SADT': f['p_sadt'],
        'HONORARIOS': f['p_hon']
    }

    for grupo, taxa in taxas_conhecidas.items():
        if taxa != 0.0:
            mask = df['TIPO_DESPESA_FINAL'] == grupo
            df.loc[mask, 'VALOR_CONCEDIDO'] *= (1 + (taxa / 100))
            
    mask_outros = ~df['TIPO_DESPESA_FINAL'].isin(taxas_conhecidas.keys())
    if f['p_outros'] != 0.0:
        df.loc[mask_outros, 'VALOR_CONCEDIDO'] *= (1 + (f['p_outros'] / 100))

    df['CUSTO_EVITADO'] = df['VALOR_SOLICITADO'] - df['VALOR_CONCEDIDO']
    return df

# =====================================================================
# ROTAS E LOGICA CORE FLASK
# =====================================================================

@app.route('/Logo_Postal-03.png')
def serve_logo():
    return send_from_directory(os.getcwd(), 'Logo_Postal-03.png')

@app.route('/')
def dashboard():
    f = {
        'comp': request.args.get('comp', '').strip(),
        'tipo_neg': request.args.get('tipo_neg', 'TODOS').strip(),
        'uf_alvo': request.args.get('uf_alvo', '').strip(),
        'cnpj_alvo': request.args.get('cnpj_alvo', '').strip(),
        'p_dietas': float(request.args.get('p_dietas', '0.00') or 0.0),
        'p_perfuro': float(request.args.get('p_perfuro', '0.00') or 0.0),
        'p_anest': float(request.args.get('p_anest', '0.00') or 0.0),
        'p_mat': float(request.args.get('p_mat', '0.00') or 0.0),
        'p_med': float(request.args.get('p_med', '0.00') or 0.0),
        'p_dia': float(request.args.get('p_dia', '0.00') or 0.0),
        'p_taxa': float(request.args.get('p_taxa', '0.00') or 0.0),
        'p_gas': float(request.args.get('p_gas', '0.00') or 0.0),
        'p_opme': float(request.args.get('p_opme', '0.00') or 0.0),
        'p_sadt': float(request.args.get('p_sadt', '0.00') or 0.0),
        'p_hon': float(request.args.get('p_hon', '0.00') or 0.0),
        'p_outros': float(request.args.get('p_outros', '0.00') or 0.0)
    }

    totais = {'faturamento_total': '0,00', 'linhas_faturamento': 0, 'total_concedido': '0,00', 'custo_evitado': '0,00'}
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
                    fat_total = df_final['VALOR_SOLICITADO'].sum()
                    con_total = df_final['VALOR_CONCEDIDO'].sum()
                    evit_total = df_final['CUSTO_EVITADO'].sum()

                    totais = {
                        'faturamento_total': f"{fat_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                        'linhas_faturamento': len(df_final),
                        'total_concedido': f"{con_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                        'custo_evitado': f"{evit_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    }

                    grupo = df_final.groupby(['TIPO_DESPESA_FINAL', 'ORIGEM']).agg(
                        qtd=('VALOR_SOLICITADO', 'count'),
                        v_sol=('VALOR_SOLICITADO', 'sum'),
                        v_con=('VALOR_CONCEDIDO', 'sum')
                    ).reset_index()

                    for _, r in grupo.iterrows():
                        itens_detalhe.append({
                            'tipo_despesa': r['TIPO_DESPESA_FINAL'],
                            'origem': r['ORIGEM'],
                            'qtd': r['qtd'],
                            'valor_sol': f"{r['v_sol']:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                            'valor_con': f"{r['v_con']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                        })
        except Exception as e:
            flash(f"Erro ao processar simulação SQL: {str(e)}", "error")

    return render_template_string(HTML_DASHBOARD, totais=totais, itens_detalhe=itens_detalhe, periodo_base=f['comp'] or "Nenhuma", comp_atual=f['comp'], filtros=f, tem_dados=tem_dados)

@app.route('/exportar')
def exportar():
    f = {
        'comp': request.args.get('comp', '').strip(),
        'tipo_neg': request.args.get('tipo_neg', 'TODOS').strip(),
        'uf_alvo': request.args.get('uf_alvo', '').strip(),
        'cnpj_alvo': request.args.get('cnpj_alvo', '').strip(),
        'p_dietas': float(request.args.get('p_dietas', '0.00')),
        'p_perfuro': float(request.args.get('p_perfuro', '0.00')),
        'p_anest': float(request.args.get('p_anest', '0.00')),
        'p_mat': float(request.args.get('p_mat', '0.00')),
        'p_med': float(request.args.get('p_med', '0.00')),
        'p_dia': float(request.args.get('p_dia', '0.00')),
        'p_taxa': float(request.args.get('p_taxa', '0.00')),
        'p_gas': float(request.args.get('p_gas', '0.00')),
        'p_opme': float(request.args.get('p_opme', '0.00')),
        'p_sadt': float(request.args.get('p_sadt', '0.00')),
        'p_hon': float(request.args.get('p_hon', '0.00')),
        'p_outros': float(request.args.get('p_outros', '0.00'))
    }
    
    if not f['comp'] or not engine: 
        return redirect(url_for('dashboard'))
    
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
            df_final.to_excel(writer, index=False, sheet_name="Reajuste_Calculado")
        output.seek(0)
        
        return send_file(output, download_name=f"Cenario_Reajuste_{f['comp']}.xlsx", as_attachment=True)
    except Exception as e:
        flash(f"Erro ao gerar Excel completo: {str(e)}", "error")
        return redirect(url_for('dashboard'))

@app.route('/admin')
def admin(): 
    status = obter_linhas_tabela()
    comps_fat = []
    
    if engine:
        try:
            with engine.connect() as conn:
                # Modificado para evitar erro se a coluna ainda não existir
                try:
                    res = conn.execute(text("SELECT \"COMPETENCIA\", COUNT(*) FROM faturamento GROUP BY \"COMPETENCIA\" ORDER BY \"COMPETENCIA\" DESC"))
                    for row in res:
                        comps_fat.append({'comp': row[0], 'linhas': row[1]})
                except:
                    pass
        except:
            pass
            
    return render_template_string(HTML_ADMIN, status_bases=status, comps_fat=comps_fat)

@app.route('/admin/limpar/<tipo_base>', methods=['POST'])
def limpar_base(tipo_base):
    if engine:
        try:
            with engine.begin() as conn: 
                conn.execute(text(f"DROP TABLE IF EXISTS {tipo_base}"))
            flash(f"Módulo [{tipo_base}] resetado com sucesso!", "success")
        except Exception as e:
            flash(f"Erro ao limpar base: {str(e)}", "error")
    return redirect(url_for('admin'))

@app.route('/admin/limpar_competencia/<comp>', methods=['POST'])
def limpar_competencia(comp):
    if engine:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"DELETE FROM faturamento WHERE \"COMPETENCIA\" = '{comp}'"))
            flash(f"Competência [{comp}] excluída cirurgicamente do histórico!", "success")
        except Exception as e:
            flash(f"Erro ao remover mês: {str(e)}", "error")
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
            
            if arquivo.filename.endswith('.parquet'): 
                df = pd.read_parquet(arquivo)
            elif arquivo.filename.endswith('.csv') or arquivo.filename.endswith('.txt'):
                amostra = arquivo.read(2048).decode('utf-8', errors='ignore')
                arquivo.seek(0)
                
                if '¬' in amostra: delimitador = '¬'
                elif ';' in amostra: delimitador = ';'
                elif '\t' in amostra: delimitador = '\t'
                else: delimitador = None
                
                try: 
                    df = pd.read_csv(arquivo, sep=delimitador, engine='python', encoding='utf-8', on_bad_lines='skip')
                except: 
                    arquivo.seek(0)
                    df = pd.read_csv(arquivo, sep=delimitador, engine='python', encoding='iso-8859-1', on_bad_lines='skip')
            elif arquivo.filename.endswith('.xlsx'): 
                df = pd.read_excel(arquivo)
            else: 
                continue

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
                    
                    # --- A VACINA: INJEÇÃO AUTOMÁTICA DA COLUNA NO BANCO ---
                    try:
                        conn.execute(text('ALTER TABLE faturamento ADD COLUMN IF NOT EXISTS "COMPETENCIA" TEXT;'))
                    except:
                        pass # Ignora silenciosamente se a tabela faturamento ainda não existir de jeito nenhum (será criada abaixo)
                    # --------------------------------------------------------

                    df.to_sql('faturamento', con=conn, if_exists='append', index=False, chunksize=200000)
                else:
                    df.to_sql(tipo_base, con=conn, if_exists='replace' if primeiro else 'append', index=False, chunksize=200000)
            
            linhas += len(df)
            primeiro = False
            
        flash(f"Sucesso! {linhas} linhas totais gravadas na tabela [{tipo_base}].", "success")
    except Exception as e:
        flash(f"Erro crítico no processamento do lote: {str(e)}", "error")
    return redirect(url_for('admin'))
