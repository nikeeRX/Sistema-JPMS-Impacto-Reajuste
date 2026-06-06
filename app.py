import os
import io
from flask import Flask, request, redirect, url_for, send_file, render_template_string, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from fpdf import FPDF
from datetime import datetime

app = Flask(__name__)

# Chave de segurança obrigatória para o sistema de Login
app.secret_key = 'chave_super_secreta_pizzaria_sp'

# Conexão com o banco PostgreSQL na Railway
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:FKCizxbIlDRCIzeewkKvmlFRBIEMLGgZ@postgres.railway.internal:5432/railway'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ==========================================
# USUÁRIOS DO SISTEMA
# ==========================================
USUARIOS = {
    'admin': 'admin123',
    'financeiro': 'finan123'
}

# ==========================================
# TABELAS DO BANCO DE DADOS
# ==========================================
class Lancamento(db.Model):
    __tablename__ = 'controle_diario_v2'
    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.String(20), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    categoria = db.Column(db.String(100))
    descricao = db.Column(db.String(255), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    
    comprovante_nome = db.Column(db.String(255), nullable=True)
    comprovante_dados = db.Column(db.LargeBinary, nullable=True)
    comprovante_mimetype = db.Column(db.String(100), nullable=True)

class CategoriaItem(db.Model):
    __tablename__ = 'categorias_lista'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), unique=True, nullable=False)

class DescricaoItem(db.Model):
    __tablename__ = 'descricoes_lista'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), unique=True, nullable=False)

with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Aguardando conexão... {e}")

# ==========================================
# TEMPLATES HTML/CSS (LOGIN, PRINCIPAL, EDITAR)
# ==========================================

CSS_PADRAO = """
    body { font-family: Arial, sans-serif; background-color: #f4f4f9; margin: 0; padding: 10px; }
    .container { max-width: 950px; margin: 0 auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
    .header { text-align: center; margin-bottom: 20px; }
    .header img { max-width: 200px; }
    h2 { color: #000; border-bottom: 2px solid #E30613; padding-bottom: 5px; }
    
    .form-group { display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 15px; }
    .form-group div { flex: 1; min-width: 150px; }
    label { display: block; font-weight: bold; margin-bottom: 5px; color: #000; }
    input, select { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; height: 44px; }
    
    .input-btn-group { display: flex; gap: 5px; align-items: center; width: 100%; }
    .btn-add { background-color: #000; color: white; border: none; cursor: pointer; font-weight: bold; border-radius: 4px; width: 50px; height: 44px; font-size: 18px; display: flex; align-items: center; justify-content: center; }
    .btn-add:hover { background-color: #333; }

    .file-upload-box { background-color: #f9f9f9; border: 2px dashed #ccc; padding: 10px; text-align: center; border-radius: 4px; margin-top: 5px; }
    .file-upload-box input[type="file"] { border: none; background: transparent; padding: 0; height: auto; }
    
    button { background-color: #E30613; color: white; border: none; padding: 12px 20px; cursor: pointer; font-weight: bold; border-radius: 4px; transition: 0.3s; font-size: 15px; width: 100%; height: 48px; }
    button:hover { background-color: #A30000; }
    .btn-pdf { background-color: #000; width: auto; margin-bottom: 10px; }
    .btn-pdf:hover { background-color: #333; }
    
    .btn-acao { color: white; padding: 6px 10px; text-decoration: none; border-radius: 4px; font-size: 12px; font-weight: bold; display: inline-block; margin: 2px; border: none; cursor: pointer; }
    .btn-anexo { background-color: #007bff; }
    .btn-editar { background-color: #ffc107; color: #000; }
    .btn-excluir { background-color: #dc3545; }
    
    table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
    th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
    th { background-color: #000; color: white; }
    tr:hover { background-color: #f1f1f1; }
    .saida { color: #E30613; font-weight: bold; }
    .entrada { color: #000; }
    
    .user-panel { display: flex; justify-content: space-between; align-items: center; background: #eee; padding: 10px; border-radius: 4px; margin-bottom: 20px; font-weight: bold; }
    .user-panel a { color: #E30613; text-decoration: none; }

    /* AJUSTES PARA MOBILE */
    @media (max-width: 600px) {
        .form-group { flex-direction: column; gap: 10px; }
        .input-btn-group select, .input-btn-group input { flex-grow: 1; }
        table { font-size: 11px; }
        th, td { padding: 6px; }
        button { font-size: 16px; height: 50px; } /* Botões maiores para dedo */
    }
"""

LOGIN_TEMPLATE = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Login - Controle Financeiro</title>
    <style>{CSS_PADRAO}</style>
</head>
<body>
<div class="container" style="max-width: 400px; margin-top: 50px;">
    <div class="header">
        <img src="/logo.png" alt="Logo São Paulo" onerror="this.style.display='none'">
        <h2>Acesso ao Sistema</h2>
    </div>
    {% if erro %}
        <p style="color: red; text-align: center;"><b>{{ erro }}</b></p>
    {% endif %}
    <form action="/login" method="POST">
        <div style="margin-bottom: 15px;">
            <label>Usuário:</label>
            <input type="text" name="usuario" required>
        </div>
        <div style="margin-bottom: 20px;">
            <label>Senha:</label>
            <input type="password" name="senha" required>
        </div>
        <button type="submit">Entrar</button>
    </form>
</div>
</body>
</html>
"""

HTML_TEMPLATE = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Controle Financeiro - São Paulo</title>
    <style>{CSS_PADRAO}</style>
    <script>
        function adicionarCategoria() {{
            let nova = prompt("Digite o nome da nova Categoria:");
            if (nova && nova.trim() !== "") {{
                window.location.href = "/nova_categoria?nome=" + encodeURIComponent(nova.trim());
            }}
        }}
        function adicionarDescricao() {{
            let nova = prompt("Digite a nova Descrição para salvar na lista:");
            if (nova && nova.trim() !== "") {{
                window.location.href = "/nova_descricao?nome=" + encodeURIComponent(nova.trim());
            }}
        }}
    </script>
</head>
<body>

<div class="container">
    <div class="user-panel">
        <span>Logado como: <span style="color:#E30613;">{{ session['usuario'].upper() }}</span></span>
        <a href="/logout">🚪 Sair do Sistema</a>
    </div>

    <div class="header">
        <img src="/logo.png" alt="Logo São Paulo" onerror="this.style.display='none'">
        <h1>Controle Financeiro</h1>
    </div>

    <h2>Novo Lançamento</h2>
    <form action="/adicionar" method="POST" enctype="multipart/form-data">
        <div class="form-group">
            <div>
                <label>Data:</label>
                <input type="text" name="data" value="{{ data_hoje }}" required>
            </div>
            <div>
                <label>Tipo:</label>
                <select name="tipo">
                    <option value="Entrada">Entrada</option>
                    <option value="Saída" selected>Saída</option>
                </select>
            </div>
            <div>
                <label>Categoria:</label>
                <div class="input-btn-group">
                    <select name="categoria" required>
                        {% for cat in categorias %}
                            <option value="{{ cat.nome }}">{{ cat.nome }}</option>
                        {% endfor %}
                    </select>
                    {% if session['usuario'] == 'admin' %}
                        <button type="button" class="btn-add" onclick="adicionarCategoria()">+</button>
                    {% endif %}
                </div>
            </div>
        </div>
        
        <div class="form-group">
            <div style="flex: 2;">
                <label>Descrição (Selecione ou digite):</label>
                <div class="input-btn-group">
                    <input list="lista-descricoes" name="descricao" required placeholder="Ex: Conta de Energia" autocomplete="off">
                    <datalist id="lista-descricoes">
                        {% for op in descricoes %}
                            <option value="{{ op.nome }}">
                        {% endfor %}
                    </datalist>
                    {% if session['usuario'] == 'admin' %}
                        <button type="button" class="btn-add" onclick="adicionarDescricao()">+</button>
                    {% endif %}
                </div>
            </div>
            <div>
                <label>Valor (R$):</label>
                <input type="number" step="0.01" name="valor" required placeholder="0.00">
            </div>
        </div>
        
        <div class="form-group">
            <div style="flex: 1;">
                <label>Nota / Comprovante (Câmera ou Arquivo):</label>
                <div class="file-upload-box">
                    <input type="file" name="comprovante" accept="image/*,application/pdf">
                </div>
            </div>
        </div>
        
        <button type="submit">Salvar Registro</button>
    </form>

    <h2 style="margin-top: 40px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
        Histórico
        <a href="/gerar_pdf"><button type="button" class="btn-pdf">Gerar PDF</button></a>
    </h2>
    
    <div style="overflow-x: auto;">
        <table>
            <thead>
                <tr>
                    <th>Data</th>
                    <th>Tipo</th>
                    <th>Categoria</th>
                    <th>Descrição</th>
                    <th>Valor</th>
                    <th>Nota</th>
                    {% if session['usuario'] == 'admin' %}
                    <th>Ações</th>
                    {% endif %}
                </tr>
            </thead>
            <tbody>
                {% for r in lancamentos %}
                <tr>
                    <td>{{ r.data }}</td>
                    <td class="{% if r.tipo == 'Saída' %}saida{% else %}entrada{% endif %}">{{ r.tipo }}</td>
                    <td>{{ r.categoria }}</td>
                    <td>{{ r.descricao }}</td>
                    <td class="{% if r.tipo == 'Saída' %}saida{% else %}entrada{% endif %}">R$ {{ "%.2f"|format(r.valor) }}</td>
                    <td>
                        {% if r.comprovante_nome %}
                            <a href="/ver_comprovante/{{ r.id }}" target="_blank" class="btn-acao btn-anexo">📄 Abrir</a>
                        {% else %}
                            -
                        {% endif %}
                    </td>
                    {% if session['usuario'] == 'admin' %}
                    <td style="min-width: 100px;">
                        <a href="/editar/{{ r.id }}" class="btn-acao btn-editar">✏️</a>
                        <form action="/deletar/{{ r.id }}" method="POST" style="display:inline;" onsubmit="return confirm('Tem certeza que deseja excluir?');">
                            <button type="submit" class="btn-acao btn-excluir">🗑️</button>
                        </form>
                    </td>
                    {% endif %}
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>

</body>
</html>
"""

EDITAR_TEMPLATE = f"""
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Editar Lançamento</title>
    <style>{CSS_PADRAO}</style>
</head>
<body>
<div class="container">
    <h2>Editar Lançamento ID: {{ lancamento.id }}</h2>
    <form action="/atualizar/{{ lancamento.id }}" method="POST">
        <div class="form-group">
            <div>
                <label>Data:</label>
                <input type="text" name="data" value="{{ lancamento.data }}" required>
            </div>
            <div>
                <label>Tipo:</label>
                <select name="tipo">
                    <option value="Entrada" {% if lancamento.tipo == 'Entrada' %}selected{% endif %}>Entrada</option>
                    <option value="Saída" {% if lancamento.tipo == 'Saída' %}selected{% endif %}>Saída</option>
                </select>
            </div>
        </div>
        <div class="form-group">
            <div style="flex: 2;">
                <label>Descrição:</label>
                <input type="text" name="descricao" value="{{ lancamento.descricao }}" required>
            </div>
            <div>
                <label>Valor (R$):</label>
                <input type="number" step="0.01" name="valor" value="{{ lancamento.valor }}" required>
            </div>
        </div>
        <div style="display: flex; gap: 10px;">
            <button type="submit" style="flex: 2;">Salvar Alterações</button>
            <a href="/" style="flex: 1;"><button type="button" style="background: #666;">Cancelar</button></a>
        </div>
    </form>
</div>
</body>
</html>
"""

# ==========================================
# ROTAS DO SISTEMA
# ==========================================

@app.before_request
def verificar_login():
    # Bloqueia rotas se não estiver logado
    rotas_livres = ['/login', '/logo.png']
    if request.path not in rotas_livres and 'usuario' not in session:
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = request.form.get('usuario')
        senha = request.form.get('senha')
        
        if user in USUARIOS and USUARIOS[user] == senha:
            session['usuario'] = user
            return redirect(url_for('index'))
        else:
            return render_template_string(LOGIN_TEMPLATE, erro="Usuário ou senha incorretos!")
            
    return render_template_string(LOGIN_TEMPLATE, erro=None)

@app.route('/logout')
def logout():
    session.pop('usuario', None)
    return redirect(url_for('login'))

@app.route('/')
def index():
    try:
        lancamentos = Lancamento.query.order_by(Lancamento.id.desc()).all()
        categorias = CategoriaItem.query.order_by(CategoriaItem.nome).all()
        descricoes = DescricaoItem.query.order_by(DescricaoItem.nome).all()
    except Exception:
        lancamentos, categorias, descricoes = [], [], []
    
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    return render_template_string(HTML_TEMPLATE, lancamentos=lancamentos, categorias=categorias, descricoes=descricoes, data_hoje=data_hoje)

@app.route('/adicionar', methods=['POST'])
def adicionar():
    data = request.form.get('data')
    tipo = request.form.get('tipo')
    categoria = request.form.get('categoria')
    descricao = request.form.get('descricao')
    
    try:
        valor = float(request.form.get('valor').replace(',', '.'))
    except ValueError:
        valor = 0.0

    arquivo = request.files.get('comprovante')
    comprovante_nome = None
    comprovante_dados = None
    comprovante_mimetype = None

    if arquivo and arquivo.filename != '':
        comprovante_nome = secure_filename(arquivo.filename)
        comprovante_mimetype = arquivo.mimetype
        comprovante_dados = arquivo.read()
    
    novo_lancamento = Lancamento(
        data=data, tipo=tipo, categoria=categoria, 
        descricao=descricao, valor=valor,
        comprovante_nome=comprovante_nome,
        comprovante_dados=comprovante_dados,
        comprovante_mimetype=comprovante_mimetype
    )
    db.session.add(novo_lancamento)
    
    if session.get('usuario') == 'admin':
        existe_desc = DescricaoItem.query.filter_by(nome=descricao).first()
        if not existe_desc and descricao.strip() != "":
            db.session.add(DescricaoItem(nome=descricao.strip()))

    db.session.commit()
    return redirect(url_for('index'))

@app.route('/deletar/<int:id>', methods=['POST'])
def deletar(id):
    if session.get('usuario') == 'admin':
        lancamento = Lancamento.query.get_or_404(id)
        db.session.delete(lancamento)
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/editar/<int:id>')
def editar(id):
    if session.get('usuario') != 'admin':
        return redirect(url_for('index'))
    lancamento = Lancamento.query.get_or_404(id)
    return render_template_string(EDITAR_TEMPLATE, lancamento=lancamento)

@app.route('/atualizar/<int:id>', methods=['POST'])
def atualizar(id):
    if session.get('usuario') == 'admin':
        lancamento = Lancamento.query.get_or_404(id)
        lancamento.data = request.form.get('data')
        lancamento.tipo = request.form.get('tipo')
        lancamento.descricao = request.form.get('descricao')
        try:
            lancamento.valor = float(request.form.get('valor').replace(',', '.'))
        except ValueError:
            pass
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/nova_categoria')
def nova_categoria():
    if session.get('usuario') == 'admin':
        nome_cat = request.args.get('nome')
        if nome_cat and not CategoriaItem.query.filter_by(nome=nome_cat).first():
            db.session.add(CategoriaItem(nome=nome_cat))
            db.session.commit()
    return redirect(url_for('index'))

@app.route('/nova_descricao')
def nova_descricao():
    if session.get('usuario') == 'admin':
        nome_desc = request.args.get('nome')
        if nome_desc and not DescricaoItem.query.filter_by(nome=nome_desc).first():
            db.session.add(DescricaoItem(nome=nome_desc))
            db.session.commit()
    return redirect(url_for('index'))

@app.route('/ver_comprovante/<int:id>')
def ver_comprovante(id):
    lancamento = Lancamento.query.get_or_404(id)
    if not lancamento.comprovante_dados:
        return "Nenhuma nota anexada.", 404
    return send_file(io.BytesIO(lancamento.comprovante_dados), mimetype=lancamento.comprovante_mimetype, as_attachment=False, download_name=lancamento.comprovante_nome)

@app.route('/logo.png')
def serve_logo():
    logo_path = os.path.join(os.getcwd(), 'logo.png')
    if os.path.exists(logo_path): return send_file(logo_path, mimetype='image/png')
    return "", 404

@app.route('/gerar_pdf')
def gerar_pdf():
    lancamentos = Lancamento.query.order_by(Lancamento.id.desc()).all()
    pdf = FPDF()
    pdf.add_page()
    logo_path = os.path.join(os.getcwd(), 'logo.png')
    if os.path.exists(logo_path): pdf.image(logo_path, x=10, y=8, w=40)
        
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 15, "Relatorio Geral de Lancamentos", ln=True, align='C')
    pdf.ln(10)
    
    pdf.set_font("Arial", 'B', 9)
    pdf.set_fill_color(0, 0, 0)
    pdf.set_text_color(255, 255, 255)
    
    pdf.cell(20, 8, "Data", border=1, fill=True, align="C")
    pdf.cell(20, 8, "Tipo", border=1, fill=True, align="C")
    pdf.cell(40, 8, "Categoria", border=1, fill=True, align="C")
    pdf.cell(85, 8, "Descricao", border=1, fill=True, align="C")
    pdf.cell(25, 8, "Valor", border=1, fill=True, align="C")
    pdf.ln()
    
    pdf.set_font("Arial", '', 9)
    tot_ent, tot_sai = 0, 0
    for r in lancamentos:
        pdf.set_text_color(0, 0, 0)
        pdf.cell(20, 8, r.data, border=1, align="C")
        if r.tipo == "Saída":
            pdf.set_text_color(227, 6, 19)
            tot_sai += r.valor
        else:
            tot_ent += r.valor
            
        pdf.cell(20, 8, r.tipo, border=1, align="C")
        pdf.cell(40, 8, str(r.categoria)[:18], border=1, align="L")
        desc_limpa = str(r.descricao).encode('latin-1', 'replace').decode('latin-1')
        pdf.cell(85, 8, desc_limpa[:40], border=1, align="L")
        pdf.cell(25, 8, f"R$ {r.valor:.2f}", border=1, align="R")
        pdf.ln()

    liquido = tot_ent - tot_sai
    pdf.ln(5)
    pdf.set_font("Arial", 'B', 11)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 7, f"Total de Entradas: R$ {tot_ent:.2f}", ln=True)
    pdf.set_text_color(227, 6, 19)
    pdf.cell(0, 7, f"Total de Saidas: R$ {tot_sai:.2f}", ln=True)
    
    pdf.set_text_color(0, 100, 0) if liquido >= 0 else pdf.set_text_color(227, 6, 19)
    pdf.cell(0, 9, f"Saldo Liquido: R$ {liquido:.2f}", ln=True)
    
    cam_pdf = "/tmp/relatorio.pdf" if os.name != 'nt' else "relatorio_temporario.pdf"
    pdf.output(cam_pdf)
    return send_file(cam_pdf, as_attachment=True, download_name=f"Relatorio_{datetime.now().strftime('%d%m%Y_%H%M')}.pdf")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
