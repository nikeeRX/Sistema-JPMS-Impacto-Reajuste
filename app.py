import os
import io
from flask import Flask, request, redirect, url_for, send_file, render_template_string
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from fpdf import FPDF
from datetime import datetime

app = Flask(__name__)

# Conexão com o banco PostgreSQL na Railway
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:FKCizxbIlDRCIzeewkKvmlFRBIEMLGgZ@postgres.railway.internal:5432/railway'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Modelo da Tabela no Banco de Dados (Atualizado para V2)
class Lancamento(db.Model):
    __tablename__ = 'controle_diario_v2'
    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.String(20), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    categoria = db.Column(db.String(100))
    descricao = db.Column(db.String(255), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    
    # Novos campos para salvar a foto/PDF direto no banco de dados
    comprovante_nome = db.Column(db.String(255), nullable=True)
    comprovante_dados = db.Column(db.LargeBinary, nullable=True)
    comprovante_mimetype = db.Column(db.String(100), nullable=True)

# Cria a tabela se não existir
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Aguardando conexão com o banco... {e}")

# Lista de sugestões extraída da sua foto
OPCOES_DESCRICAO = [
    "Repasse Couvert", "Contabilidade", "Aluguel", "Aluguel Impressora", 
    "Água", "Água Nagão", "Energia", "Mais Network (Gestão Ifood)", 
    "Marketing", "Nutricionista", "Manutenção Fornos", "Sistema Nyte", 
    "Sistema de Notas", "Conta Celular", "Internet", "Folha de Pagamento", 
    "Vale-Transporte", "Extras", "Secador mãos", "Dedetização", 
    "INSS", "FGTS", "Juros CH", "SIMPLES"
]

# ==========================================
# INTERFACE HTML/CSS EMBUTIDA
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Controle Financeiro - São Paulo</title>
    <style>
        body { font-family: Arial, sans-serif; background-color: #f4f4f9; margin: 0; padding: 20px; }
        .container { max-width: 950px; margin: 0 auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        .header { text-align: center; margin-bottom: 20px; }
        .header img { max-width: 200px; }
        h2 { color: #000; border-bottom: 2px solid #E30613; padding-bottom: 5px; }
        
        .form-group { display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 15px; }
        .form-group div { flex: 1; min-width: 150px; }
        label { display: block; font-weight: bold; margin-bottom: 5px; color: #000; }
        input, select { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
        
        .file-upload-box { background-color: #f9f9f9; border: 2px dashed #ccc; padding: 15px; text-align: center; border-radius: 4px; margin-top: 5px;}
        .file-upload-box input[type="file"] { border: none; background: transparent; padding: 0; }
        
        button { background-color: #E30613; color: white; border: none; padding: 12px 20px; cursor: pointer; font-weight: bold; border-radius: 4px; transition: 0.3s; font-size: 15px; width: 100%;}
        button:hover { background-color: #A30000; }
        .btn-pdf { background-color: #000; width: auto; margin-bottom: 10px; }
        .btn-pdf:hover { background-color: #333; }
        .btn-anexo { background-color: #007bff; color: white; padding: 6px 10px; text-decoration: none; border-radius: 4px; font-size: 12px; }
        .btn-anexo:hover { background-color: #0056b3; }
        
        table { width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 14px;}
        th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background-color: #000; color: white; }
        tr:hover { background-color: #f1f1f1; }
        .saida { color: #E30613; font-weight: bold; }
        .entrada { color: #000; }
    </style>
</head>
<body>

<div class="container">
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
                    <option value="Saída">Saída</option>
                </select>
            </div>
            <div>
                <label>Categoria:</label>
                <select name="categoria">
                    <option value="Faturamento do Dia">Faturamento do Dia</option>
                    <option value="Pagamento Fornecedor">Pagamento Fornecedor</option>
                    <option value="Despesas Administrativas">Despesas Administrativas</option>
                    <option value="Tecnologia da Informação">Tecnologia da Informação</option>
                    <option value="Recursos Humanos">Recursos Humanos</option>
                    <option value="Outros">Outros</option>
                </select>
            </div>
        </div>
        
        <div class="form-group">
            <div style="flex: 2;">
                <label>Descrição (Comece a digitar para ver as sugestões):</label>
                <input list="lista-descricoes" name="descricao" required placeholder="Ex: Conta de Energia">
                <datalist id="lista-descricoes">
                    {% for op in opcoes_desc %}
                        <option value="{{ op }}">
                    {% endfor %}
                </datalist>
            </div>
            <div>
                <label>Valor (R$):</label>
                <input type="text" name="valor" required placeholder="0.00">
            </div>
        </div>
        
        <div class="form-group">
            <div style="flex: 1;">
                <label>Comprovante / Foto da Nota (Opcional):</label>
                <div class="file-upload-box">
                    <input type="file" name="comprovante" accept="image/*,application/pdf">
                </div>
            </div>
        </div>
        
        <button type="submit">Salvar Registro</button>
    </form>

    <h2 style="margin-top: 40px; display: flex; justify-content: space-between; align-items: center;">
        Histórico de Lançamentos
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
                    <th>Comprovante</th>
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
                            <a href="/ver_comprovante/{{ r.id }}" target="_blank" class="btn-anexo">📄 Ver</a>
                        {% else %}
                            -
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

# ==========================================
# ROTAS DO SISTEMA
# ==========================================

@app.route('/')
def index():
    try:
        lancamentos = Lancamento.query.order_by(Lancamento.id.desc()).all()
    except Exception:
        lancamentos = []
    
    data_hoje = datetime.now().strftime("%d/%m/%Y")
    return render_template_string(HTML_TEMPLATE, lancamentos=lancamentos, data_hoje=data_hoje, opcoes_desc=OPCOES_DESCRICAO)

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

    # Lida com o upload do arquivo
    arquivo = request.files.get('comprovante')
    comprovante_nome = None
    comprovante_dados = None
    comprovante_mimetype = None

    if arquivo and arquivo.filename != '':
        comprovante_nome = secure_filename(arquivo.filename)
        comprovante_mimetype = arquivo.mimetype
        comprovante_dados = arquivo.read() # Salva o binário puro da foto
    
    novo_lancamento = Lancamento(
        data=data, tipo=tipo, categoria=categoria, 
        descricao=descricao, valor=valor,
        comprovante_nome=comprovante_nome,
        comprovante_dados=comprovante_dados,
        comprovante_mimetype=comprovante_mimetype
    )
    
    db.session.add(novo_lancamento)
    db.session.commit()
    
    return redirect(url_for('index'))

@app.route('/ver_comprovante/<int:id_lancamento>')
def ver_comprovante(id_lancamento):
    # Rota que puxa a foto do banco de dados e mostra na tela
    lancamento = Lancamento.query.get_or_404(id_lancamento)
    if not lancamento.comprovante_dados:
        return "Nenhum comprovante anexado a este lançamento.", 404
        
    return send_file(
        io.BytesIO(lancamento.comprovante_dados),
        mimetype=lancamento.comprovante_mimetype,
        as_attachment=False,
        download_name=lancamento.comprovante_nome
    )

@app.route('/logo.png')
def serve_logo():
    logo_path = os.path.join(os.getcwd(), 'logo.png')
    if os.path.exists(logo_path):
        return send_file(logo_path, mimetype='image/png')
    return "", 404

@app.route('/gerar_pdf')
def gerar_pdf():
    lancamentos = Lancamento.query.order_by(Lancamento.id.desc()).all()
    
    pdf = FPDF()
    pdf.add_page()
    
    logo_path = os.path.join(os.getcwd(), 'logo.png')
    if os.path.exists(logo_path):
        pdf.image(logo_path, x=10, y=8, w=40)
        
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
    total_entradas = 0
    total_saidas = 0
    
    for r in lancamentos:
        pdf.set_text_color(0, 0, 0)
        pdf.cell(20, 8, r.data, border=1, align="C")
        
        if r.tipo == "Saída":
            pdf.set_text_color(227, 6, 19)
            total_saidas += r.valor
        else:
            pdf.set_text_color(0, 0, 0)
            total_entradas += r.valor
            
        pdf.cell(20, 8, r.tipo, border=1, align="C")
        pdf.cell(40, 8, str(r.categoria)[:18], border=1, align="L")
        
        desc_limpa = str(r.descricao).encode('latin-1', 'replace').decode('latin-1')
        pdf.cell(85, 8, desc_limpa[:40], border=1, align="L")
        
        pdf.cell(25, 8, f"R$ {r.valor:.2f}", border=1, align="R")
        pdf.ln()

    liquido = total_entradas - total_saidas
    
    pdf.ln(5)
    pdf.set_font("Arial", 'B', 11)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 7, f"Total de Entradas: R$ {total_entradas:.2f}", ln=True)
    pdf.set_text_color(227, 6, 19)
    pdf.cell(0, 7, f"Total de Saidas: R$ {total_saidas:.2f}", ln=True)
    
    if liquido >= 0:
        pdf.set_text_color(0, 100, 0)
    else:
        pdf.set_text_color(227, 6, 19)
    pdf.cell(0, 9, f"Saldo Liquido: R$ {liquido:.2f}", ln=True)
    
    caminho_pdf = "/tmp/relatorio.pdf" if os.name != 'nt' else "relatorio_temporario.pdf"
    pdf.output(caminho_pdf)
    
    return send_file(caminho_pdf, as_attachment=True, download_name=f"Relatorio_{datetime.now().strftime('%d%m%Y_%H%M')}.pdf")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
