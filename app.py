import os
import io
import csv
from flask import Flask, request, redirect, url_for, send_file, render_template_string, session, Response
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from fpdf import FPDF
from datetime import datetime

app = Flask(__name__)

# Chave de segurança
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
    
    # Colunas antigas (mantidas apenas por segurança, não serão mais a principal)
    comprovante_nome = db.Column(db.String(255), nullable=True)
    comprovante_dados = db.Column(db.LargeBinary, nullable=True)
    comprovante_mimetype = db.Column(db.String(100), nullable=True)
    
    # Ligação com a nova tabela de múltiplos anexos
    anexos = db.relationship('Anexo', backref='lancamento', lazy=True, cascade="all, delete-orphan")

class Anexo(db.Model):
    __tablename__ = 'anexos_v2'
    id = db.Column(db.Integer, primary_key=True)
    lancamento_id = db.Column(db.Integer, db.ForeignKey('controle_diario_v2.id', ondelete='CASCADE'), nullable=False)
    nome_arquivo = db.Column(db.String(255), nullable=False)
    dados = db.Column(db.LargeBinary, nullable=False)
    mimetype = db.Column(db.String(100), nullable=False)

class CategoriaItem(db.Model):
    __tablename__ = 'categorias_lista'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), unique=True, nullable=False)

class DescricaoItem(db.Model):
    __tablename__ = 'descricoes_lista'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), unique=True, nullable=False)

class LogEdicao(db.Model):
    __tablename__ = 'logs_edicao'
    id = db.Column(db.Integer, primary_key=True)
    lancamento_id = db.Column(db.Integer, nullable=False)
    usuario = db.Column(db.String(50), nullable=False)
    data_hora = db.Column(db.String(20), nullable=False)
    detalhes = db.Column(db.Text, nullable=False)

with app.app_context():
    try:
        db.create_all()
        # MIGRACÃO AUTOMÁTICA: Move os anexos antigos para a nova tabela
        antigos = Lancamento.query.filter(Lancamento.comprovante_nome != None).all()
        for r in antigos:
            existe = Anexo.query.filter_by(lancamento_id=r.id, nome_arquivo=r.comprovante_nome).first()
            if not existe:
                novo_anexo = Anexo(
                    lancamento_id=r.id,
                    nome_arquivo=r.comprovante_nome,
                    dados=r.comprovante_dados,
                    mimetype=r.comprovante_mimetype
                )
                db.session.add(novo_anexo)
                r.comprovante_nome = None
                r.comprovante_dados = None
                r.comprovante_mimetype = None
        db.session.commit()
    except Exception as e:
        print(f"Aguardando conexão... {e}")

# ==========================================
# CSS PADRÃO
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
    input, select { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; height: 44px; font-size: 15px;}
    
    .input-btn-group { display: flex; gap: 5px; align-items: center; width: 100%; }
    .btn-add { background-color: #000; color: white; border: none; cursor: pointer; font-weight: bold; border-radius: 4px; width: 50px; height: 44px; font-size: 18px; display: flex; align-items: center; justify-content: center; }
    .btn-add:hover { background-color: #333; }

    .file-upload-box { background-color: #f9f9f9; border: 2px dashed #ccc; padding: 10px; text-align: center; border-radius: 4px; margin-top: 5px; }
    .file-upload-box input[type="file"] { border: none; background: transparent; padding: 0; height: auto; }
    
    button { background-color: #E30613; color: white; border: none; padding: 12px 20px; cursor: pointer; font-weight: bold; border-radius: 4px; transition: 0.3s; font-size: 15px; width: 100%; height: 48px; }
    button:hover { background-color: #A30000; }
    .btn-acao-top { width: auto; margin-bottom: 10px; }
    
    .btn-acao { color: white; padding: 6px 10px; text-decoration: none; border-radius: 4px; font-size: 12px; font-weight: bold; display: inline-block; margin: 2px; border: none; cursor: pointer; text-align: center;}
    .btn-anexo { background-color: #007bff; }
    .btn-editar { background-color: #ffc107; color: #000; }
    .btn-excluir { background-color: #dc3545; }
    
    table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
    th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
    th { background-color: #000; color: white; }
    tr:hover { background-color: #f1f1f1; }
    .saida { color: #E30613; font-weight: bold; }
    .entrada { color: #000; }
    
    .user-panel { display: flex; justify-content: space-between; align-items: center; background: #eee; padding: 10px; border-radius: 4px; margin-bottom: 20px; font-weight: bold; flex-wrap: wrap; gap: 10px;}
    .user-panel a { color: #E30613; text-decoration: none; }

    .select2-container .select2-selection--single { height: 44px !important; border: 1px solid #ccc !important; border-radius: 4px !important; padding-top: 6px !important; font-size: 15px; }
    .select2-container--default .select2-selection--single .select2-selection__arrow { height: 42px !important; }
    .select2-container--default .select2-selection--single .select2-selection__rendered { color: #000 !important; line-height: 30px !important; }

    .box-filtros { background: #fdfdfd; padding: 15px; border-radius: 4px; border: 1px solid #ddd; margin-bottom: 20px; }
    .box-filtros h3 { margin-top: 0; color: #E30613; font-size: 15px; margin-bottom: 10px; }

    .lista-anexos { list-style: none; padding: 0; margin: 0; }
    .lista-anexos li { background: #fff; border: 1px solid #ddd; padding: 10px; margin-bottom: 5px; border-radius: 4px; display: flex; justify-content: space-between; align-items: center;}

    @media (max-width: 600px) {
        .form-group { flex-direction: column; gap: 10px; }
        .input-btn-group select, .input-btn-group input { flex-grow: 1; }
        .select2-container { width: 100% !important; flex-grow: 1; }
        table { font-size: 11px; }
        th, td { padding: 6px; }
        button { font-size: 16px; height: 50px; }
    }
"""

# ==========================================
# TEMPLATES HTML
# ==========================================
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Login - Controle Financeiro</title>
    <style>""" + CSS_PADRAO + """</style>
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

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Controle Financeiro - São Paulo</title>
    <link href="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/css/select2.min.css" rel="stylesheet" />
    <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/js/select2.min.js"></script>
    <style>""" + CSS_PADRAO + """</style>
    <script>
        function adicionarCategoria() {
            let nova = prompt("Digite o nome da nova Categoria:");
            if (nova && nova.trim() !== "") {
                window.location.href = "/nova_categoria?nome=" + encodeURIComponent(nova.trim());
            }
        }
        function adicionarDescricao() {
            let nova = prompt("Digite a nova Descrição para salvar na lista:");
            if (nova && nova.trim() !== "") {
                window.location.href = "/nova_descricao?nome=" + encodeURIComponent(nova.trim());
            }
        }
        $(document).ready(function() {
            $('.caixa-busca').select2({
                placeholder: "Clique ou digite para buscar...",
                width: '100%',
                language: { noResults: function() { return "Nenhuma descrição encontrada"; } }
            });
        });
    </script>
</head>
<body>
<div class="container">
    <div class="user-panel">
        <span>Logado como: <span style="color:#E30613;">{{ session['usuario'].upper() }}</span></span>
        <div>
            {% if session['usuario'] == 'admin' %}
            <a href="/logs" style="margin-right: 15px; color: #000;">📋 Ver Auditoria</a>
            {% endif %}
            <a href="/logout">🚪 Sair do Sistema</a>
        </div>
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
                <input type="text" name="data" value="{{ data_hoje }}" required placeholder="DD/MM/AAAA">
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
                <label>Descrição (Selecione ou digite para buscar):</label>
                <div class="input-btn-group">
                    <select class="caixa-busca" name="descricao" required>
                        <option value="" disabled selected></option>
                        {% for op in descricoes %}
                            <option value="{{ op.nome }}">{{ op.nome }}</option>
                        {% endfor %}
                    </select>
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
                <label>Anexos (Pode selecionar VÁRIAS notas/fotos):</label>
                <div class="file-upload-box">
                    <input type="file" name="comprovantes" accept="image/*,application/pdf" multiple>
                </div>
            </div>
        </div>
        <button type="submit">Salvar Registro</button>
    </form>

    <h2 style="margin-top: 40px;">Histórico Geral</h2>

    {% if session['usuario'] == 'admin' %}
    <div class="box-filtros">
        <h3>Filtro para Exportação (Opcional)</h3>
        <form action="/gerar_pdf" method="GET" style="display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; margin: 0;">
            <div>
                <label style="font-size: 12px;">Data Início:</label>
                <input type="date" name="inicio" style="height: 35px; width: 140px;">
            </div>
            <div>
                <label style="font-size: 12px;">Data Fim:</label>
                <input type="date" name="fim" style="height: 35px; width: 140px;">
            </div>
            <div style="display: flex; gap: 5px;">
                <button type="submit" class="btn-acao-top" style="background-color: #000; color: white; height: 35px; margin: 0; padding: 5px 15px; width: auto;">📄 Exportar PDF</button>
                <button type="submit" formaction="/exportar_csv" class="btn-acao-top" style="background-color: #217346; color: white; height: 35px; margin: 0; padding: 5px 15px; width: auto;">📊 Exportar Excel</button>
            </div>
        </form>
        <small style="color: #666; font-size: 11px;">*Se deixar em branco, exporta todo o histórico.</small>
    </div>
    {% endif %}
    
    <div style="overflow-x: auto;">
        <table>
            <thead>
                <tr>
                    <th>Data</th>
                    <th>Tipo</th>
                    <th>Categoria</th>
                    <th>Descrição</th>
                    <th>Valor</th>
                    <th>Anexos</th>
                    <th>Ações</th>
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
                        {% if r.anexos %}
                            <div style="display:flex; flex-direction:column; gap:4px;">
                            {% for a in r.anexos %}
                                <a href="/ver_anexo/{{ a.id }}" target="_blank" class="btn-acao btn-anexo">📄 {{ loop.index }}</a>
                            {% endfor %}
                            </div>
                        {% else %}
                            -
                        {% endif %}
                    </td>
                    <td style="min-width: 100px; vertical-align: top;">
                        <a href="/editar/{{ r.id }}" class="btn-acao btn-editar" style="display: block; margin-bottom: 5px;">✏️ Editar</a>
                        {% if session['usuario'] == 'admin' %}
                        <form action="/deletar/{{ r.id }}" method="POST" style="display:inline; margin:0;" onsubmit="return confirm('Tem certeza que deseja excluir este lançamento e TODAS as notas anexadas?');">
                            <button type="submit" class="btn-acao btn-excluir" style="width: 100%;">🗑️ Excluir</button>
                        </form>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
</body>
</html>
"""

EDITAR_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Editar Lançamento</title>
    <style>""" + CSS_PADRAO + """</style>
</head>
<body>
<div class="container">
    <h2>Editar Lançamento ID: {{ lancamento.id }}</h2>
    
    <div style="background: #fdfdfd; padding: 15px; border: 1px solid #ddd; border-radius: 4px; margin-bottom: 20px;">
        <h3 style="margin-top: 0;">Anexos Salvos</h3>
        {% if lancamento.anexos %}
            <ul class="lista-anexos">
            {% for a in lancamento.anexos %}
                <li>
                    <a href="/ver_anexo/{{ a.id }}" target="_blank" style="color: #007bff; text-decoration: none; font-weight: bold;">📄 {{ a.nome_arquivo }}</a>
                    <form action="/deletar_anexo/{{ a.id }}" method="POST" style="margin: 0;" onsubmit="return confirm('Tem certeza que deseja excluir definitivamente este anexo?');">
                        <button type="submit" style="background: none; border: none; color: red; font-weight: bold; cursor: pointer; padding: 0; width: auto; height: auto;">❌ Excluir</button>
                    </form>
                </li>
            {% endfor %}
            </ul>
        {% else %}
            <p style="color: #666; margin-bottom:0;">Nenhum anexo salvo para este lançamento.</p>
        {% endif %}
    </div>

    <form action="/atualizar/{{ lancamento.id }}" method="POST" enctype="multipart/form-data">
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
        
        <div class="form-group">
            <div style="flex: 1;">
                <label>Adicionar NOVO(S) Anexo(s) (Opcional):</label>
                <div class="file-upload-box">
                    <input type="file" name="novos_comprovantes" accept="image/*,application/pdf" multiple>
                </div>
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

LOGS_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Auditoria - Logs de Edição</title>
    <style>""" + CSS_PADRAO + """</style>
</head>
<body>
<div class="container">
    <h2>Logs de Edição (Auditoria)</h2>
    <div style="margin-bottom: 20px;">
        <a href="/"><button type="button" style="background: #000; width: auto; height: 35px; padding: 5px 15px;">⬅ Voltar ao Início</button></a>
    </div>
    
    <div style="overflow-x: auto;">
        <table>
            <thead>
                <tr>
                    <th>Data/Hora da Edição</th>
                    <th>Usuário</th>
                    <th>ID</th>
                    <th>O que foi alterado (De -> Para)</th>
                </tr>
            </thead>
            <tbody>
                {% for log in logs %}
                <tr>
                    <td style="white-space: nowrap; font-weight: bold;">{{ log.data_hora }}</td>
                    <td style="font-weight: bold; color: #E30613;">{{ log.usuario.upper() }}</td>
                    <td>{{ log.lancamento_id }}</td>
                    <td>{{ log.detalhes }}</td>
                </tr>
                {% endfor %}
                {% if not logs %}
                <tr><td colspan="4" style="text-align:center;">Nenhuma edição foi registrada ainda no sistema.</td></tr>
                {% endif %}
            </tbody>
        </table>
    </div>
</div>
</body>
</html>
"""

# ==========================================
# FUNÇÕES DE APOIO
# ==========================================
def buscar_lancamentos_filtrados(inicio_str, fim_str):
    lancamentos_db = Lancamento.query.order_by(Lancamento.id.desc()).all()
    if not inicio_str or not fim_str:
        return lancamentos_db
        
    filtrados = []
    try:
        data_in = datetime.strptime(inicio_str, '%Y-%m-%d').date()
        data_out = datetime.strptime(fim_str, '%Y-%m-%d').date()
        for r in lancamentos_db:
            try:
                r_date = datetime.strptime(r.data, '%d/%m/%Y').date()
                if data_in <= r_date <= data_out:
                    filtrados.append(r)
            except ValueError:
                pass
        return filtrados
    except ValueError:
        return lancamentos_db

# ==========================================
# ROTAS DO SISTEMA
# ==========================================

@app.before_request
def verificar_login():
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

    novo_lancamento = Lancamento(data=data, tipo=tipo, categoria=categoria, descricao=descricao, valor=valor)
    
    # Processa múltiplos arquivos
    arquivos = request.files.getlist('comprovantes')
    for arquivo in arquivos:
        if arquivo and arquivo.filename != '':
            novo_anexo = Anexo(
                nome_arquivo=secure_filename(arquivo.filename),
                mimetype=arquivo.mimetype,
                dados=arquivo.read()
            )
            novo_lancamento.anexos.append(novo_anexo)
            
    db.session.add(novo_lancamento)
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
    lancamento = Lancamento.query.get_or_404(id)
    return render_template_string(EDITAR_TEMPLATE, lancamento=lancamento)

@app.route('/atualizar/<int:id>', methods=['POST'])
def atualizar(id):
    lancamento = Lancamento.query.get_or_404(id)
    val_antigo = {
        'data': lancamento.data,
        'tipo': lancamento.tipo,
        'descricao': lancamento.descricao,
        'valor': lancamento.valor
    }
    
    nova_data = request.form.get('data')
    novo_tipo = request.form.get('tipo')
    nova_desc = request.form.get('descricao')
    try:
        novo_valor = float(request.form.get('valor').replace(',', '.'))
    except ValueError:
        novo_valor = val_antigo['valor']

    mudancas = []
    if val_antigo['data'] != nova_data: mudancas.append(f"Data: {val_antigo['data']} -> {nova_data}")
    if val_antigo['tipo'] != novo_tipo: mudancas.append(f"Tipo: {val_antigo['tipo']} -> {novo_tipo}")
    if val_antigo['descricao'] != nova_desc: mudancas.append(f"Descrição: '{val_antigo['descricao']}' -> '{nova_desc}'")
    if val_antigo['valor'] != novo_valor: mudancas.append(f"Valor: R${val_antigo['valor']} -> R${novo_valor}")

    # Processa novos anexos durante a edição
    arquivos = request.files.getlist('novos_comprovantes')
    qtd_novos = 0
    for arquivo in arquivos:
        if arquivo and arquivo.filename != '':
            novo_anexo = Anexo(
                nome_arquivo=secure_filename(arquivo.filename),
                mimetype=arquivo.mimetype,
                dados=arquivo.read()
            )
            lancamento.anexos.append(novo_anexo)
            qtd_novos += 1
            
    if qtd_novos > 0:
        mudancas.append(f"Adicionou {qtd_novos} novo(s) anexo(s)")

    if mudancas:
        lancamento.data = nova_data
        lancamento.tipo = novo_tipo
        lancamento.descricao = nova_desc
        lancamento.valor = novo_valor
        
        log = LogEdicao(
            lancamento_id=lancamento.id,
            usuario=session['usuario'],
            data_hora=datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            detalhes=" | ".join(mudancas)
        )
        db.session.add(log)
        db.session.commit()

    return redirect(url_for('index'))

@app.route('/deletar_anexo/<int:id_anexo>', methods=['POST'])
def deletar_anexo(id_anexo):
    anexo = Anexo.query.get_or_404(id_anexo)
    lancamento_id = anexo.lancamento_id
    
    log = LogEdicao(
        lancamento_id=lancamento_id,
        usuario=session['usuario'],
        data_hora=datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        detalhes=f"Excluiu o anexo: {anexo.nome_arquivo}"
    )
    db.session.add(log)
    db.session.delete(anexo)
    db.session.commit()
    
    return redirect(url_for('editar', id=lancamento_id))

@app.route('/logs')
def logs():
    if session.get('usuario') != 'admin':
        return redirect(url_for('index'))
    logs_bd = LogEdicao.query.order_by(LogEdicao.id.desc()).all()
    return render_template_string(LOGS_TEMPLATE, logs=logs_bd)

@app.route('/exportar_csv')
def exportar_csv():
    if session.get('usuario') != 'admin':
        return redirect(url_for('index'))
        
    inicio = request.args.get('inicio')
    fim = request.args.get('fim')
    lancamentos = buscar_lancamentos_filtrados(inicio, fim)
    
    si = io.StringIO()
    cw = csv.writer(si, delimiter=';')
    cw.writerow(['ID', 'Data', 'Tipo', 'Categoria', 'Descricao', 'Valor (R$)'])
    
    for r in lancamentos:
        valor_br = f"{r.valor:.2f}".replace('.', ',')
        cw.writerow([r.id, r.data, r.tipo, r.categoria, r.descricao, valor_br])
    
    output = si.getvalue().encode('utf-8-sig')
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=Exportacao_Financeiro_{datetime.now().strftime('%d%m%Y_%H%M')}.csv"}
    )

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

@app.route('/ver_anexo/<int:id>')
def ver_anexo(id):
    anexo = Anexo.query.get_or_404(id)
    return send_file(io.BytesIO(anexo.dados), mimetype=anexo.mimetype, as_attachment=False, download_name=anexo.nome_arquivo)

@app.route('/logo.png')
def serve_logo():
    logo_path = os.path.join(os.getcwd(), 'logo.png')
    if os.path.exists(logo_path): return send_file(logo_path, mimetype='image/png')
    return "", 404

@app.route('/gerar_pdf')
def gerar_pdf():
    if session.get('usuario') != 'admin':
        return redirect(url_for('index'))
        
    inicio = request.args.get('inicio')
    fim = request.args.get('fim')
    lancamentos = buscar_lancamentos_filtrados(inicio, fim)
    
    pdf = FPDF()
    pdf.add_page()
    logo_path = os.path.join(os.getcwd(), 'logo.png')
    if os.path.exists(logo_path): pdf.image(logo_path, x=10, y=8, w=40)
        
    pdf.set_font("Arial", 'B', 16)
    
    if inicio and fim:
        texto_cabecalho = f"Relatorio de Lancamentos ({datetime.strptime(inicio, '%Y-%m-%d').strftime('%d/%m/%Y')} a {datetime.strptime(fim, '%Y-%m-%d').strftime('%d/%m/%Y')})"
    else:
        texto_cabecalho = "Relatorio Geral de Lancamentos"
        
    pdf.cell(0, 15, texto_cabecalho, ln=True, align='C')
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
    pdf.cell(0, 7, f"Total de Entradas no Periodo: R$ {tot_ent:.2f}", ln=True)
    pdf.set_text_color(227, 6, 19)
    pdf.cell(0, 7, f"Total de Saidas no Periodo: R$ {tot_sai:.2f}", ln=True)
    
    pdf.set_text_color(0, 100, 0) if liquido >= 0 else pdf.set_text_color(227, 6, 19)
    pdf.cell(0, 9, f"Saldo Liquido do Periodo: R$ {liquido:.2f}", ln=True)
    
    cam_pdf = "/tmp/relatorio.pdf" if os.name != 'nt' else "relatorio_temporario.pdf"
    pdf.output(cam_pdf)
    return send_file(cam_pdf, as_attachment=True, download_name=f"Relatorio_{datetime.now().strftime('%d%m%Y_%H%M')}.pdf")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
