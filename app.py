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
        
        # Garante a preservação de colunas estratégicas solicitadas pela área de negócio
        for col in ['UF', 'AP']:
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
# BANCO DE DADOS E STATUS
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
# INTERFACE HTML (VISUAL COMPLETO POSTAL SAÚDE)
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
    .metric-box { padding: 15px; border: 1px solid #eee; border-radius: 6px; text-align: center; }
    .metric-box h4 { margin: 0 0 10px 0; color: #666; font-size: 0.9em; text-transform: uppercase; }
    .metric-box .valor { font-size: 1.5em; font-weight: bold; color: var(--azul-postal); margin-bottom: 5px; }
    .metric-box .sub { font-size: 0.85em; color: #888; }
    .impacto-card { background-color: #fff9e6; border-top: 4px solid var(--amarelo-postal); }
    table { width: 100%; border-collapse: collapse; margin-top: 15px; }
    th { background-color: var(--azul-postal); color: white; padding: 12px; text-align: left; border-bottom: 3px solid var(--amarelo-postal); }
    td { padding: 10px 12px; border-bottom: 1px solid #eee; }
    tr:hover { background-color: #f4f7f6; }
    .btn { background-color: var(--amarelo-postal); color: var(--azul-postal); font-weight: bold; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
    .btn:hover { background-color: #e0a100; }
    .btn-success { background-color: var(--verde-ok); color: white; }
    .btn-success:hover { background-color: #004d20; }
    .form-group { margin-bottom: 15px; }
    .form-group label { display: block; margin-bottom: 5px; font-weight: bold; color: var(--azul-postal); }
    .alert { padding: 15px; border-radius: 4px; margin-bottom: 20px; }
    .alert-success { background: #e6f4ea; color: var(--verde-ok); border: 1px solid var(--verde-ok); }
    .alert-danger { background: #fde8e8; color: var(--vermelho-alerta); border: 1px solid var(--vermelho-alerta); }
</style>
"""

HTML_DASHBOARD = CSS_PADRAO + """
<div class="header">
    <div style="display: flex; align-items: center; gap: 20px;">
        <img src="/Logo_Postal-03.png" class="logo-img">
        <div>
            <h2 style="margin:0;">GERED - Sistema de Impacto de Reajuste</h2>
            <p style="margin:5px 0 0 0; color: #d4e3ef;">Competência Analisada: <strong>{{ periodo_base }}</strong></p>
        </div>
    </div>
    <a href="/admin" class="btn">Painel Admin (Uploads / Gerenciador)</a>
</div>

<div class="container">
    <div class="card" style="background-color: var(--azul-postal); color: white; border-top: none;">
        <h3 style="margin-top:0; color: var(--amarelo-postal);">Painel de Controle: Executar Cruzamentos</h3>
        <form action="/" method="get" style="display: flex; gap: 15px; align-items: flex-end;">
            <div class="form-group" style="margin-bottom: 0; flex: 1;">
                <label style="color: white;">Digite a Competência Alvo salva no banco (Ex: 2026-04):</label>
                <input type="text" name="comp" style="width: 100%; padding: 10px; border-radius: 4px;" value="{{ comp_atual }}" placeholder="YYYY-MM" required>
            </div>
            <button type="submit" class="btn" style="height: 38px;">Processar Colunas</button>
        </form>
    </div>

    <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h3 style="margin:0; color: var(--azul-postal);">Resumo Financeiro Real</h3>
            {% if tem_dados %}
            <a href="/exportar?comp={{ comp_atual }}" class="btn btn-success">📥 Baixar Planilha Final Cruzada (Excel)</a>
            {% endif %}
        </div>
        
        <div class="grid-4" style="margin-top: 15px;">
            <div class="metric-box">
                <h4>Faturamento Total</h4>
                <div class="valor">R$ {{ totais.faturamento_total }}</div>
                <div class="sub">{{ totais.linhas_faturamento }} linhas processadas</div>
            </div>
            <div class="metric-box">
                <h4>Itens em Dotação</h4>
                <div class="valor" style="color: var(--azul-claro);">R$ {{ totais.total_dotacao }}</div>
            </div>
            <div class="metric-box">
                <h4>Faixas de Eventos</h4>
                <div class="valor" style="color: var(--amarelo-postal);">R$ {{ totais.total_faixa }}</div>
            </div>
            <div class="metric-box impacto-card">
                <h4>Desconto (Primeira Linha)</h4>
                <div class="valor" style="color: var(--verde-ok);">R$ {{ totais.total_desconto }}</div>
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
                    <td colspan="4" style="text-align: center; color: #888; padding: 30px;">Nenhum faturamento cruzado para esta competência. Busque acima ou vá ao Painel Admin para carregar.</td>
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
            <form action="/admin_upload" method="post" enctype="multipart/form-data">
                <div class="form-group">
                    <label>Selecione a Base de Destino:</label>
                    <select name="tipo_base" style="width: 100%; padding: 10px;" required>
                        <option value="faturamento">Faturamento Mensal</option>
                        <option value="dotacoes">Base de Dotações</option>
                        <option value="materiais">Materials Perfurocortantes</option>
                        <option value="dietas">Dietas</option>
                        <option value="faixas">Faixa de Eventos</option>
                        <option value="prestadores">Prestadores</option>
                    </select>
                </div>
                
                <div class="form-group">
                    <label>Competência (Obrigatório apenas para Faturamento):</label>
                    <input type="text" name="competencia" style="width: 100%; padding: 10px;" placeholder="Ex: 2026-04">
                </div>

                <div class="form-group">
                    <label>Selecione a PASTA INTEIRA com os arquivos (Upload total):</label>
                    <input type="file" name="arquivos_pasta" style="width: 100%; padding: 15px; background: #fafafa; border: 2px dashed #005a92;" webkitdirectory directory multiple>
                </div>
                
                <div class="form-group">
                    <label>OU Selecione arquivos soltos manualmente (Segure CTRL):</label>
                    <input type="file" name="arquivos_soltos" style="width: 100%; padding: 15px; background: #fafafa; border: 2px dashed #999;" multiple>
                </div>
                
                <button type="submit" class="btn btn-success" style="width: 100%; font-size: 16px; padding: 12px;">Injetar Dados no Servidor</button>
            </form>
        </div>

        <div class="card" style="border-top: 4px solid var(--amarelo-postal);">
            <h3 style="margin-top:0; color: var(--azul-postal);">Gerenciador de Tabelas Ativas</h3>
            <table>
                <thead><tr><th>Tabela</th><th>Status</th><th>Linhas</th><th>Ação</th></tr></thead>
                <tbody>
                    {% for t_id, info in status_bases.items() %}
                    <tr>
                        <td><strong>{{ info.desc }}</strong></td>
                        <td style="color:{% if info.linhas > 0 %}var(--verde-ok){% else %}#999{% endif %}; font-weight:bold;">{{ info.status }}</td>
                        <td>{{ info.linhas }}</td>
                        <td>
                            {% if info.linhas > 0 %}
                            <form action="/admin/limpar/{{ t_id }}" method="post" style="margin:0;"><button type="submit" style="background:var(--vermelho-alerta); color:white; border:none; padding:5px 10px; border-radius:4px; cursor:pointer;">Limpar</button></form>
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
# ROTAS E LOGICA CORE
# =====================================================================
@app.route('/Logo_Postal-03.png')
def serve_logo():
    return send_from_directory(os.getcwd(), 'Logo_Postal-03.png')

@app.route('/')
def dashboard():
    comp = request.args.get('comp', '').strip()
    totais = {'faturamento_total': '0,00', 'linhas_faturamento': 0, 'total_dotacao': '0,00', 'total_faixa': '0,00', 'total_desconto': '0,00'}
    itens_detalhe = []
    tem_dados = False
    
    if comp and engine:
        try:
            df_fat = pd.read_sql(text(f"SELECT * FROM faturamento WHERE \"COMPETENCIA\" = '{comp}'"), con=engine)
            if not df_fat.empty:
                # Substituídos os blocos bugados por Python Try/Except seguro
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

                # Executa processamento real das colunas
                df_resultado = cruzar_bases(df_fat, df_mat, df_die, df_dot, df_fai, df_pre)
                
                if not df_resultado.empty:
                    tem_dados = True
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

                        # RESTAURADO: Lógica real de agregação para montar a tabela do Dashboard
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

    return render_template_string(HTML_DASHBOARD, totais=totais, itens_detalhe=itens_detalhe, periodo_base=comp or "Nenhuma selecionada", comp_atual=comp, tem_dados=tem_dados)

@app.route('/exportar')
def exportar():
    comp = request.args.get('comp', '').strip()
    if not comp or not engine: return redirect(url_for('dashboard'))
    
    try:
        df_fat = pd.read_sql(text(f"SELECT * FROM faturamento WHERE \"COMPETENCIA\" = '{comp}'"), con=engine)
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
        
        df_final = cruzar_bases(df_fat, df_mat, df_die, df_dot, df_fai, df_pre)
        
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df_final.to_excel(writer, index=False, sheet_name=f"Cruzamento_{comp}")
        output.seek(0)
        
        return send_file(output, download_name=f"Resultado_Cruzamento_{comp}.xlsx", as_attachment=True)
    except Exception as e:
        flash(f"Erro ao gerar Excel: {str(e)}", "error")
        return redirect(url_for('dashboard'))

@app.route('/admin')
def admin(): 
    return render_template_string(HTML_ADMIN, status_bases=obter_linhas_tabela())

@app.route('/admin/limpar/<tipo_base>', methods=['POST'])
def limpar_base(tipo_base):
    if engine:
        try:
            with engine.begin() as conn: 
                conn.execute(text(f"DROP TABLE IF EXISTS {tipo_base}"))
            flash(f"Base de dados [{tipo_base}] limpa com sucesso!", "success")
        except Exception as e:
            flash(f"Erro ao limpar base: {str(e)}", "error")
    return redirect(url_for('admin'))

@app.route('/admin_upload', methods=['POST'])
def admin_upload():
    # Coleta arquivos de ambas as formas de seleção (pasta e soltos)
    arquivos = request.files.getlist('arquivos_pasta') + request.files.getlist('arquivos_soltos')
    tipo_base = request.form.get('tipo_base')
    competencia = request.form.get('competencia')
    
    if not arquivos or all(a.filename == '' for a in arquivos):
        flash("Nenhum arquivo enviado!", "error")
        return redirect(url_for('admin'))

    linhas = 0
    primeiro = True
    try:
        for arquivo in arquivos:
            if arquivo.filename == '': continue
            if arquivo.filename.endswith('.parquet'): df = pd.read_parquet(arquivo)
            elif arquivo.filename.endswith('.csv') or arquivo.filename.endswith('.txt'):
                try: df = pd.read_csv(arquivo, sep=None, engine='python', encoding='utf-8')
                except: 
                    arquivo.seek(0)
                    df = pd.read_csv(arquivo, sep=None, engine='python', encoding='iso-8859-1')
            elif arquivo.filename.endswith('.xlsx'): df = pd.read_excel(arquivo)
            else: continue

            if df.empty: continue

            for col in ['UF', 'AP']:
                if col not in df.columns: df[col] = None
            
            if 'VLR_DESCONTO_OBTIDO' in df.columns:
                df['VLR_DESCONTO_OBTIDO'] = pd.to_numeric(df['VLR_DESCONTO_OBTIDO'], errors='coerce').fillna(0)
                tot = df['VLR_DESCONTO_OBTIDO'].sum()
                df['VLR_DESCONTO_OBTIDO'] = 0.0
                df.at[df.index[0], 'VLR_DESCONTO_OBTIDO'] = tot

            with engine.begin() as conn:
                if tipo_base == 'faturamento':
                    if competencia: df['COMPETENCIA'] = competencia
                    df.to_sql('faturamento', con=conn, if_exists='append', index=False)
                else:
                    df.to_sql(tipo_base, con=conn, if_exists='replace' if primeiro else 'append', index=False)
            linhas += len(df)
            primeiro = False
            
        flash(f"Sucesso! {linhas} linhas totais processadas e salvas na base [{tipo_base}].", "success")
    except Exception as e:
        flash(f"Erro no processamento de lote: {str(e)}", "error")
    return redirect(url_for('admin'))
