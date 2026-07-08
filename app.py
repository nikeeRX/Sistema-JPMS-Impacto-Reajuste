import os
import re
import traceback
import unicodedata
import pandas as pd
import numpy as np
from flask import Flask, request, render_template_string, redirect, url_for, flash, send_from_directory
from sqlalchemy import create_engine, text
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

# Helper para ver o status real das tabelas no banco de dados
def obter_linhas_tabela():
    resumos = {}
    mapeamento = {
        'faturamento': 'Faturamento Mensal',
        'prestadores': 'Cadastro de Prestadores',
        'materiais': 'Materiais Perfurocortantes',
        'dietas': 'Tabela de Dietas',
        'dotacoes': 'Base de Dotações',
        'faixas': 'Faixa de Eventos'
    }
    for t_nome, t_desc in mapeamento.items():
        if not engine:
            resumos[t_nome] = {'desc': t_desc, 'status': 'Sem Conexão', 'linhas': 0}
            continue
        try:
            with engine.connect() as conn:
                res = conn.execute(text(f"SELECT COUNT(*) FROM {t_nome}"))
                qtd = res.scalar()
                resumos[t_nome] = {'desc': t_desc, 'status': 'Ativa / Carregada', 'linhas': qtd}
        except:
            resumos[t_nome] = {'desc': t_desc, 'status': 'Vazia / Aguardando', 'linhas': 0}
    return resumos

# =====================================================================
# CSS E HTML EMBUTIDOS (DESIGN POSTAL SAÚDE COMPLETO)
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
    
    table { width: 100%; border-collapse: collapse; margin-top: 15px; }
    th { background-color: var(--azul-postal); color: white; padding: 12px; text-align: left; border-bottom: 3px solid var(--amarelo-postal); }
    td { padding: 10px 12px; border-bottom: 1px solid #eee; }
    tr:hover { background-color: #f4f7f6; }
    
    .btn { background-color: var(--amarelo-postal); color: var(--azul-postal); font-weight: bold; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; transition: background-color 0.3s; }
    .btn:hover { background-color: #e0a100; }
    .btn-success { background-color: var(--azul-claro); color: white; font-weight: normal; }
    .btn-success:hover { background-color: var(--azul-postal); }
    .btn-danger { background-color: var(--vermelho-alerta); color: white; padding: 6px 12px; font-size: 0.85em; border-radius: 4px; }
    .btn-danger:hover { background-color: #990000; }
    
    .form-group { margin-bottom: 15px; }
    .form-group label { display: block; margin-bottom: 5px; font-weight: bold; color: var(--azul-postal); }
    .alert { padding: 15px; border-radius: 4px; margin-bottom: 20px; }
    .alert-success { background: #e6f4ea; color: var(--verde-ok); border: 1px solid var(--verde-ok); }
    .alert-danger { background: #fde8e8; color: var(--vermelho-alerta); border: 1px solid var(--vermelho-alerta); }
    .alert-info { background: #e3f2fd; color: var(--azul-postal); border: 1px solid var(--azul-claro); }
</style>
"""

HTML_DASHBOARD = CSS_PADRAO + """
<div class="header">
    <div class="header-logo-container">
        <img src="/Logo_Postal-03.png" class="logo-img" alt="Postal Saúde">
        <div>
            <h2 style="margin:0;">GERED - Sistema de Impacto de Reajuste</h2>
            <p style="margin:5px 0 0 0; color: #d4e3ef;">Competência Analisada: <strong>{{ periodo_base }}</strong></p>
        </div>
    </div>
    <a href="/admin" class="btn">Painel Admin (Uploads / Excluir)</a>
</div>

<div class="container">
    {% with messages = get_flashed_messages(category_filter=["info", "error"]) %}
      {% if messages %}
        {% for message in messages %}
          <div class="alert alert-info"><strong>Status:</strong> {{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div class="card" style="background-color: var(--azul-postal); color: white; border-top: none;">
        <h3 style="margin-top:0; color: var(--amarelo-postal);">Painel de Controle: Executar Cruzamentos</h3>
        <form action="/" method="get" style="display: flex; gap: 15px; align-items: flex-end;">
            <div class="form-group" style="margin-bottom: 0; flex: 1;">
                <label style="color: white;">Selecione a Competência Salva no Banco (Ex: 2026-04):</label>
                <input type="text" name="comp" style="width: 100%; padding: 10px; border-radius: 4px; border: none; box-sizing: border-box;" placeholder="YYYY-MM" value="{{ comp_atual }}" required>
            </div>
            <button type="submit" class="btn" style="height: 38px;">Cruzar Bases e Calcular Impacto</button>
        </form>
    </div>

    <div class="card">
        <h3 style="margin-top:0; color: var(--azul-postal);">Resumo Financeiro Real (Competência: {{ periodo_base }})</h3>
        <div class="grid-4">
            <div class="metric-box">
                <h4>Faturamento Total Lido</h4>
                <div class="valor">R$ {{ totais.faturamento_total }}</div>
                <div class="sub">{{ totais.linhas_faturamento }} linhas em faturamento</div>
            </div>
            <div class="metric-box">
                <h4>Itens em Dotação</h4>
                <div class="valor" style="color: var(--azul-claro);">R$ {{ totais.total_dotacao }}</div>
                <div class="sub">Casamento por Chave Única</div>
            </div>
            <div class="metric-box">
                <h4>Faixas de Eventos</h4>
                <div class="valor" style="color: var(--amarelo-postal);">R$ {{ totais.total_faixa }}</div>
                <div class="sub">Regras por Faixas Aplicadas</div>
            </div>
            <div class="metric-box impacto-card">
                <h4>Desconto Concentrado</h4>
                <div class="valor" style="color: var(--verde-ok);">R$ {{ totais.total_desconto }}</div>
                <div class="sub">Valor consolidado em linha única</div>
            </div>
        </div>
    </div>

    <div class="card">
        <h3 style="margin-top:0; color: var(--azul-postal);">Detalhamento por Grupo de Despesa Final</h3>
        <table>
            <thead>
                <tr>
                    <th>Grupo de Despesa (TIPO_DESPESA_FINAL)</th>
                    <th>Origem da Regra (ORIGEM)</th>
                    <th>Qtd de Itens</th>
                    <th>Valor Total Pago (R$)</th>
                </tr>
            </thead>
            <tbody>
                {% for item in itens_detalhe %}
                <tr>
                    <td><strong>{{ item.tipo_despesa }}</strong></td>
                    <td><span style="background: #eef2f5; padding: 4px 8px; border-radius: 4px; font-size:0.9em;">{{ item.origem }}</span></td>
                    <td>{{ item.qtd }}</td>
                    <td style="color: var(--azul-postal); font-weight: bold;">R$ {{ item.valor }}</td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="4" style="text-align: center; color: #888; padding: 30px;">Nenhum faturamento cruzado para esta competência. Vá no Painel Admin para alimentar as tabelas.</td>
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
    {% with messages = get_flashed_messages(category_filter=["success"]) %}
      {% if messages %}
        {% for message in messages %}
          <div class="alert alert-success"><strong>Sucesso:</strong> {{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    {% with messages = get_flashed_messages(category_filter=["error"]) %}
      {% if messages %}
        {% for message in messages %}
          <div class="alert alert-danger"><strong>Erro:</strong> {{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
        
        <div class="card" style="border-top: 4px solid var(--azul-claro);">
            <h3 style="margin-top:0; color: var(--azul-postal);">Upload em Massa de Arquivos</h3>
            <form action="/admin_upload" method="post" enctype="multipart/form-data">
                <div class="form-group">
                    <label>Selecione a Base de Destino:</label>
                    <select name="tipo_base" style="width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px;" required>
                        <option value="faturamento">Faturamento (Mês a Mês)</option>
                        <option value="prestadores">Prestadores</option>
                        <option value="materiais">Materiais Perfurocortantes</option>
                        <option value="dietas">Dietas</option>
                        <option value="dotacoes">Base de Dotações</option>
                        <option value="faixas">Faixa de Eventos</option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>Competência (Ex: 2026-04) - *Obrigatório apenas para Faturamento*:</label>
                    <input type="text" name="competencia" style="width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px;" placeholder="YYYY-MM">
                </div>

                <div class="form-group">
                    <label>Arraste ou Selecione os Arquivos (Aceita múltiplos Parquet, CSV, TXT ou Excel):</label>
                    <input type="file" name="arquivos_upload" style="width: 100%; padding: 20px; border: 2px dashed var(--azul-claro); border-radius: 4px; background: #fafafa;" multiple required>
                </div>
                
                <button type="submit" class="btn btn-success" style="width: 100%; font-size: 16px; padding: 12px;">Processar e Injetar no Lote</button>
            </form>
        </div>

        <div class="card" style="border-top: 4px solid var(--amarelo-postal);">
            <h3 style="margin-top:0; color: var(--azul-postal);">Tabelas Ativas no PostgreSQL</h3>
            <p style="font-size: 0.9em; color: #666; margin-bottom: 15px;">Confira abaixo quais bases já possuem dados e limpe-as se precisar recarregar.</p>
            
            <table>
                <thead>
                    <tr>
                        <th>Tabela / Base</th>
                        <th>Status</th>
                        <th>Qtd Linhas</th>
                        <th>Ação</th>
                    </tr>
                </thead>
                <tbody>
                    {% for t_id, info in status_bases.items() %}
                    <tr>
                        <td>
                            <strong>{{ info.desc }}</strong><br>
                            <span style="font-family:monospace; font-size:0.8em; color:#777;">{{ t_id }}</span>
                        </td>
                        <td>
                            {% if info.linhas > 0 %}
                            <span style="color: var(--verde-ok); font-weight: bold;">● {{ info.status }}</span>
                            {% else %}
                            <span style="color: #999;">○ {{ info.status }}</span>
                            {% endif %}
                        </td>
                        <td>{{ info.linhas }}</td>
                        <td>
                            {% if info.linhas > 0 %}
                            <form action="/admin/limpar/{{ t_id }}" method="post" onsubmit="return confirm('Tem certeza que quer apagar TODOS os dados da base de {{ info.desc }}?');" style="margin:0;">
                                <button type="submit" class="btn-danger">Excluir</button>
                            </form>
                            {% else %}
                            <button class="btn-danger" style="background:#ccc; cursor:not-allowed;" disabled>Vazia</button>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>

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
    comp = request.args.get('comp', '').strip()
    totais = {'faturamento_total': '0,00', 'linhas_faturamento': 0, 'total_dotacao': '0,00', 'total_faixa': '0,00', 'total_desconto': '0,00'}
    itens_detalhe = []
    
    if comp and engine:
        try:
            df_fat = pd.read_sql(text(f"SELECT * FROM faturamento WHERE \"COMPETENCIA\" = '{comp}'"), con=engine)
            
            if not df_fat.empty:
                def carregar_tabela_safe(nome):
                    try: return pd.read_sql(text(f"SELECT * FROM {nome}"), con=engine)
                    except: return pd.DataFrame()

                df_mat = carregar_tabela_safe('materiais')
                df_die = carregar_tabela_safe('dietas')
                df_dot = carregar_tabela_safe('dotacoes')
                df_fai = carregar_tabela_safe('faixas')
                df_pre = carregar_tabela_safe('prestadores')

                # Executa o algoritmo real de cruzamento de colunas
                df_resultado = cruzar_bases(df_fat, df_mat, df_die, df_dot, df_fai, df_pre)

                if not df_resultado.empty:
                    v_col = _find_column(df_resultado, [COL_VALOR_PAGO, 'VALOR_PAG', 'VALOR_PAGO', 'VALOR'])
                    if v_col:
                        df_resultado[v_col] = pd.to_numeric(df_resultado[v_col], errors='coerce').fillna(0)
                        
                        fat_total = df_resultado[v_col].sum()
                        tot_dot = df_resultado[df_resultado['ORIGEM'] == 'Dotação'][v_col].sum()
                        tot_fai = df_resultado[df_resultado['ORIGEM'] == 'Faixa de Evento'][v_col].sum()
                        
                        tot_desc = 0.0
                        if 'VLR_DESCONTO_OBTIDO' in df_resultado.columns:
                            tot_desc = pd.to_numeric(df_resultado['VLR_DESCONTO_OBTIDO'], errors='coerce').fillna(0).sum()

                        totais = {
                            'faturamento_total': f"{fat_total:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                            'linhas_faturamento': len(df_resultado),
                            'total_dotacao': f"{tot_dot:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                            'total_faixa': f"{tot_fai:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                            'total_desconto': f"{tot_desc:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                        }

                        # Agrupamento das colunas por Regra e Tipo de Despesa
                        grupo = df_resultado.groupby(['TIPO_DESPESA_FINAL', 'ORIGEM']).agg(
                            qtd=(v_col, 'count'),
                            valor_total=(v_col, 'sum')
                        ).reset_index()

                        for _, r in grupo.iterrows():
                            itens_detalhe.append({
                                'tipo_despesa': r['TIPO_DESPESA_FINAL'],
                                'origem': r['ORIGEM'],
                                'qtd': r['qtd'],
                                'valor': f"{r['valor_total']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                            })
        except Exception as e:
            flash(f"Erro no processamento SQL: {str(e)}", "error")

    return render_template_string(HTML_DASHBOARD, totais=totais, itens_detalhe=itens_detalhe, periodo_base=comp if comp else "Aguardando Entrada", comp_atual=comp)

@app.route('/admin')
def admin():
    status = obter_linhas_tabela()
    return render_template_string(HTML_ADMIN, status_bases=status)

# ROTA ADICIONADA: Permite apagar a tabela selecionada para reimportar sem erro
@app.route('/admin/limpar/<tipo_base>', methods=['POST'])
def limpar_base(tipo_base):
    if not engine:
        flash("Erro: Conexão ausente.", "error")
        return redirect(url_for('admin'))
    try:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {tipo_base}"))
        flash(f"A base de dados [{tipo_base}] foi totalmente excluída do PostgreSQL!", "success")
    except Exception as e:
        flash(f"Erro ao limpar banco: {str(e)}", "error")
    return redirect(url_for('admin'))

@app.route('/admin_upload', methods=['POST'])
def admin_upload():
    # Coleta todos os arquivos independente de barreiras do formulário HTML
    arquivos = []
    for chave in request.files:
        arquivos.extend(request.files.getlist(chave))
        
    tipo_base = request.form.get('tipo_base')
    competencia = request.form.get('competencia')
    
    if not arquivos or arquivos[0].filename == '':
        flash("Nenhum arquivo selecionado!", "error")
        return redirect(url_for('admin'))

    if not engine:
        flash("Erro crítico: Banco PostgreSQL desconectado!", "error")
        return redirect(url_for('admin'))

    linhas_totais = 0
    arquivos_sucesso = 0
    primeiro_do_lote = True

    try:
        for arquivo in arquivos:
            if arquivo.filename == '':
                continue
                
            if arquivo.filename.endswith('.parquet'):
                df = pd.read_parquet(arquivo)
            elif arquivo.filename.endswith('.csv') or arquivo.filename.endswith('.txt'):
                try:
                    df = pd.read_csv(arquivo, sep=None, engine='python', encoding='utf-8')
                except UnicodeDecodeError:
                    arquivo.seek(0)
                    df = pd.read_csv(arquivo, sep=None, engine='python', encoding='iso-8859-1')
            elif arquivo.filename.endswith('.xlsx'):
                df = pd.read_excel(arquivo)
            else:
                continue

            if df.empty:
                continue

            # Garante colunas obrigatórias de região (UF e AP)
            for col in ['UF', 'AP']:
                if col not in df.columns:
                    df[col] = None
            
            # Preserva a integridade do desconto consolidado na primeira linha
            if 'VLR_DESCONTO_OBTIDO' in df.columns:
                df['VLR_DESCONTO_OBTIDO'] = pd.to_numeric(df['VLR_DESCONTO_OBTIDO'], errors='coerce').fillna(0)
                total_desconto = df['VLR_DESCONTO_OBTIDO'].sum()
                df['VLR_DESCONTO_OBTIDO'] = 0.0
                df.at[df.index[0], 'VLR_DESCONTO_OBTIDO'] = total_desconto

            with engine.begin() as conn:
                if tipo_base == 'faturamento':
                    if competencia:
                        df['COMPETENCIA'] = competencia
                    df.to_sql('faturamento', con=conn, if_exists='append', index=False)
                else:
                    modo = 'replace' if primeiro_do_lote else 'append'
                    df.to_sql(tipo_base, con=conn, if_exists=modo, index=False)

            linhas_totais += len(df)
            arquivos_sucesso += 1
            primeiro_do_lote = False

        if arquivos_sucesso > 0:
            flash(f"Injetados {arquivos_sucesso} arquivo(s) na base [{tipo_base}] com sucesso! Total de {linhas_totais} linhas gravadas no PostgreSQL.", "success")
        else:
            flash("Nenhum arquivo aceito foi encontrado no lote.", "error")

    except Exception as e:
        flash(f"Erro ao processar lote: {str(e)}", "error")

    return redirect(url_for('admin'))
