import os
from flask import Flask, request, redirect, url_for, send_file, render_template_string
from flask_sqlalchemy import SQLAlchemy
from fpdf import FPDF
from datetime import datetime

app = Flask(__name__)

# Conexão com o banco PostgreSQL na Railway (URL interna que você enviou)
app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://postgres:FKCizxbIlDRCIzeewkKvmlFRBIEMLGgZ@postgres.railway.internal:5432/railway'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Modelo da Tabela no Banco de Dados
class Lancamento(db.Model):
    __tablename__ = 'controle_diario'
    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.String(20), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    categoria = db.Column(db.String(100))
    descricao = db.Column(db.String(255), nullable=False)
    valor = db.Column(db.Float, nullable=False)

# Cria a tabela se não existir (garante o funcionamento na primeira vez)
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Aguardando conexão com o banco... {e}")

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
        .container { max-width: 900px; margin: 0 auto; background: #fff; padding: 20px; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
        .header { text-align: center; margin-bottom: 20px; }
        .header img { max-width: 200px; }
        h2 { color: #000; border-bottom: 2px solid #E30613; padding-bottom: 5px; }
        
        .form-group { display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 15px; }
        .form-group div { flex: 1; min-width: 150px; }
        label { display: block; font-weight: bold; margin-bottom: 5px; color: #000; }
        input, select { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
        
        button { background-color: #E30613; color: white; border: none; padding: 10px 20px; cursor: pointer; font-weight: bold; border-radius: 4px; transition: 0.3s; }
        button:hover { background-color: #A30000; }
        .btn-pdf { background-color: #000; margin-bottom: 10px; }
        .btn-pdf:hover { background-color: #333; }
        
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }
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
    <form action="/adicionar" method="POST">
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
                    <option value="Contas Fixas">Contas Fixas</option>
                    <option value="Outros">Outros</option>
                </select>
            </div>
        </div>
        <div class="form-group">
            <div style="flex: 2;">
                <label>Descrição:</label>
                <input type="text" name="descricao" required placeholder="Ex: Pagamento fornecedor de queijo">
            </div>
            <div>
                <label>Valor (R$):</label>
                <input type="text" name="valor" required placeholder="0.00">
            </div>
        </div>
        <button type="submit">Salvar Registro</button>
    </form>

    <h2 style="margin-top: 40px; display: flex; justify-content: space-between; align-items: center;">
        Histórico de Lançamentos
        <a href="/gerar_pdf"><button type="button" class="btn-pdf">Gerar PDF</button></a>
    </h2>
    
    <table>
        <thead>
            <tr>
                <th>Data</th>
                <th>Tipo</th>
                <th>Categoria</th>
                <th>Descrição</th>
                <th>Valor</th>
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
            </tr>
            {% endfor %}
        </tbody>
    </table>
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
    return render_template_string(HTML_TEMPLATE, lancamentos=lancamentos, data_hoje=data_hoje)

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
    
    novo_lancamento = Lancamento(
        data=data, tipo=tipo, categoria=categoria, 
        descricao=descricao, valor=valor
    )
    
    db.session.add(novo_lancamento)
    db.session.commit()
    
    return redirect(url_for('index'))

@app.route('/logo.png')
def serve_logo():
    # Rota que serve a logo na raiz do site
    logo_path = os.path.join(os.getcwd(), 'logo.png')
    if os.path.exists(logo_path):
        return send_file(logo_path, mimetype='image/png')
    return "", 404

@app.route('/gerar_pdf')
def gerar_pdf():
    lancamentos = Lancamento.query.order_by(Lancamento.id.desc()).all()
    
    pdf = FPDF()
    pdf.add_page()
    
    # Tenta inserir a logo no PDF se ela existir na mesma pasta do app.py
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
        
        # Limpa caracteres não suportados pela fonte base do FPDF
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
        pdf.set_text_color(0, 100, 0) # Verde se positivo
    else:
        pdf.set_text_color(227, 6, 19)
    pdf.cell(0, 9, f"Saldo Liquido: R$ {liquido:.2f}", ln=True)
    
    caminho_pdf = "/tmp/relatorio.pdf" if os.name != 'nt' else "relatorio_temporario.pdf"
    pdf.output(caminho_pdf)
    
    return send_file(caminho_pdf, as_attachment=True, download_name=f"Relatorio_{datetime.now().strftime('%d%m%Y_%H%M')}.pdf")

if __name__ == '__main__':
    # Porta dinâmica para serviços de cloud (como Railway)
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
