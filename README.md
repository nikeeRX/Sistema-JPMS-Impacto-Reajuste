import tkinter as tk
from tkinter import ttk, messagebox
from fpdf import FPDF
from datetime import datetime
import os
import sqlite3

# Nome do arquivo de banco de dados local
DB_NOME = "banco_local.db"

def inicializar_banco():
    """Cria o banco de dados local e a tabela se não existirem"""
    conn = sqlite3.connect(DB_NOME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lancamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            tipo TEXT,
            categoria TEXT,
            descricao TEXT,
            valor REAL
        )
    ''')
    conn.commit()
    conn.close()

class AppFinanceiro:
    def __init__(self, root):
        self.root = root
        self.root.title("Controle Financeiro - São Paulo")
        self.root.geometry("750x650")
        self.root.configure(bg="#FFFFFF")

        # Configuração visual (Paleta da Logo: Vermelho, Preto e Branco)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TNotebook", background="#FFFFFF", borderwidth=0)
        style.configure("TNotebook.Tab", background="#000000", foreground="#FFFFFF", font=("Arial", 10, "bold"), padding=[10, 5])
        style.map("TNotebook.Tab", background=[("selected", "#E30613")], foreground=[("selected", "#FFFFFF")])
        
        style.configure("TLabel", background="#FFFFFF", foreground="#000000", font=("Arial", 10, "bold"))
        style.configure("TButton", background="#E30613", foreground="#FFFFFF", font=("Arial", 10, "bold"))
        style.map("TButton", background=[("active", "#A30000")])
        style.configure("Treeview.Heading", font=("Arial", 10, "bold"), background="#000000", foreground="#FFFFFF")

        # Criando as Abas
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, y=(10, 10))

        self.aba_cadastro = tk.Frame(self.notebook, bg="#FFFFFF")
        self.aba_historico = tk.Frame(self.notebook, bg="#FFFFFF")

        self.notebook.add(self.aba_cadastro, text=" Novo Lançamento ")
        self.notebook.add(self.aba_historico, text=" Histórico Geral ")

        self.configurar_aba_cadastro()
        self.configurar_aba_historico()
        
        # Carrega os dados existentes na aba de histórico ao iniciar
        self.atualizar_treeview_historico()

    def configurar_aba_cadastro(self):
        # Frame de Inputs
        frame_inputs = tk.Frame(self.aba_cadastro, bg="#FFFFFF")
        frame_inputs.pack(pady=30, padx=20, fill="x")

        # Data
        ttk.Label(frame_inputs, text="Data:").grid(row=0, column=0, sticky="w", pady=8)
        self.ent_data = ttk.Entry(frame_inputs, width=15)
        self.ent_data.insert(0, datetime.now().strftime("%d/%m/%Y"))
        self.ent_data.grid(row=0, column=1, sticky="w", pady=8, padx=5)

        # Tipo
        ttk.Label(frame_inputs, text="Tipo:").grid(row=0, column=2, sticky="w", pady=8)
        self.combo_tipo = ttk.Combobox(frame_inputs, values=["Entrada", "Saída"], width=15, state="readonly")
        self.combo_tipo.current(0)
        self.combo_tipo.grid(row=0, column=3, sticky="w", pady=8, padx=5)

        # Categoria
        ttk.Label(frame_inputs, text="Categoria:").grid(row=1, column=0, sticky="w", pady=8)
        categorias = ["Faturamento do Dia", "Pagamento Fornecedor", "Contas Fixas", "Outros"]
        self.combo_cat = ttk.Combobox(frame_inputs, values=categorias, width=25)
        self.combo_cat.current(0)
        self.combo_cat.grid(row=1, column=1, columnspan=3, sticky="w", pady=8, padx=5)

        # Descrição
        ttk.Label(frame_inputs, text="Descrição:").grid(row=2, column=0, sticky="w", pady=8)
        self.ent_desc = ttk.Entry(frame_inputs, width=50)
        self.ent_desc.grid(row=2, column=1, columnspan=3, sticky="w", pady=8, padx=5)

        # Valor
        ttk.Label(frame_inputs, text="Valor (R$):").grid(row=3, column=0, sticky="w", pady=8)
        self.ent_valor = ttk.Entry(frame_inputs, width=15)
        self.ent_valor.grid(row=3, column=1, sticky="w", pady=8, padx=5)

        # Botão Salvar
        btn_salvar = ttk.Button(frame_inputs, text="Salvar no Sistema", command=self.salvar_registro)
        btn_salvar.grid(row=4, column=1, columnspan=3, sticky="w", pady=20)

        # Container para a logo decorativa na tela de cadastro
        if os.path.exists("logo.png"):
            try:
                self.img_logo = tk.PhotoImage(file="logo.png").subsample(2, 2)
                lbl_logo = tk.Label(self.aba_cadastro, image=self.img_logo, bg="#FFFFFF")
                lbl_logo.pack(pady=20)
            except Exception:
                pass

    def configurar_aba_historico(self):
        # Tabela de histórico
        colunas = ("ID", "Data", "Tipo", "Categoria", "Descrição", "Valor")
        self.tree_hist = ttk.Treeview(self.aba_historico, columns=colunas, show="headings", height=15)
        
        self.tree_hist.heading("ID", text="ID")
        self.tree_hist.heading("Data", text="Data")
        self.tree_hist.heading("Tipo", text="Tipo")
        self.tree_hist.heading("Categoria", text="Categoria")
        self.tree_hist.heading("Descrição", text="Descrição")
        self.tree_hist.heading("Valor", text="Valor")

        self.tree_hist.column("ID", width=40, anchor="center")
        self.tree_hist.column("Data", width=90, anchor="center")
        self.tree_hist.column("Tipo", width=90, anchor="center")
        self.tree_hist.column("Categoria", width=140, anchor="w")
        self.tree_hist.column("Descrição", width=220, anchor="w")
        self.tree_hist.column("Valor", width=100, anchor="center")
        
        self.tree_hist.pack(pady=20, padx=20, fill="both", expand=True)

        # Frame de ações na parte inferior da aba de histórico
        frame_acoes = tk.Frame(self.aba_historico, bg="#FFFFFF")
        frame_acoes.pack(fill="x", pady=10, padx=20)

        btn_atualizar = ttk.Button(frame_acoes, text="Atualizar Lista", command=self.atualizar_treeview_historico)
        btn_atualizar.pack(side="left", padx=5)

        btn_pdf = ttk.Button(frame_acoes, text="Gerar Relatório PDF do Histórico", command=self.gerar_pdf_historico)
        btn_pdf.pack(side="right", padx=5)

    def salvar_registro(self):
        data = self.ent_data.get()
        tipo = self.combo_tipo.get()
        cat = self.combo_cat.get()
        desc = self.ent_desc.get()
        
        try:
            valor = float(self.ent_valor.get().replace(",", "."))
        except ValueError:
            messagebox.showerror("Erro", "Insira um valor numérico válido.")
            return

        if not desc:
            messagebox.showwarning("Aviso", "Preencha a descrição do lançamento.")
            return

        # Grava no banco SQLite local
        conn = sqlite3.connect(DB_NOME)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO lancamentos (data, tipo, categoria, descricao, valor)
            VALUES (?, ?, ?, ?, ?)
        ''', (data, tipo, cat, desc, valor))
        conn.commit()
        conn.close()

        messagebox.showinfo("Sucesso", "Lançamento incluído com sucesso no histórico!")
        
        # Limpa os campos de texto
        self.ent_desc.delete(0, tk.END)
        self.ent_valor.delete(0, tk.END)
        
        # Atualiza a aba de histórico automaticamente
        self.atualizar_treeview_historico()

    def obter_todos_registros(self):
        conn = sqlite3.connect(DB_NOME)
        cursor = conn.cursor()
        cursor.execute("SELECT id, data, tipo, categoria, descricao, valor FROM lancamentos ORDER BY id DESC")
        linhas = cursor.fetchall()
        conn.close()
        return linhas

    def atualizar_treeview_historico(self):
        # Limpa os dados antigos da tabela visual
        for item in self.tree_hist.get_children():
            self.tree_hist.delete(item)
            
        # Puxa os dados atualizados do banco local
        registros = self.obter_todos_registros()
        for r in registros:
            self.tree_hist.insert("", "end", values=(r[0], r[1], r[2], r[3], r[4], f"R$ {r[5]:.2f}"))

    def gerar_pdf_historico(self):
        registros = self.obter_todos_registros()
        if not registros:
            messagebox.showinfo("Aviso", "Nenhum registro encontrado no histórico para gerar o PDF.")
            return

        pdf = FPDF()
        pdf.add_page()

        # Adiciona a imagem da logo se ela existir localmente
        if os.path.exists("logo.png"):
            pdf.image("logo.png", x=10, y=8, w=40)

        pdf.set_font("Arial", 'B', 16)
        pdf.cell(0, 15, "Relatório Geral de Lançamentos", ln=True, align='C')
        pdf.ln(10)

        # Cabeçalhos do PDF
        pdf.set_font("Arial", 'B', 9)
        pdf.set_fill_color(0, 0, 0)
        pdf.set_text_color(255, 255, 255)
        
        pdf.cell(20, 8, "Data", border=1, fill=True, align="C")
        pdf.cell(20, 8, "Tipo", border=1, fill=True, align="C")
        pdf.cell(40, 8, "Categoria", border=1, fill=True, align="C")
        pdf.cell(85, 8, "Descrição", border=1, fill=True, align="C")
        pdf.cell(25, 8, "Valor", border=1, fill=True, align="C")
        pdf.ln()

        pdf.set_font("Arial", '', 9)
        pdf.set_text_color(0, 0, 0)
        
        total_entradas = 0
        total_saidas = 0

        for r in registros:
            # Estrutura do banco: (id, data, tipo, categoria, descricao, valor)
            data_reg, tipo_reg, cat_reg, desc_reg, valor_reg = r[1], r[2], r[3], r[4], r[5]
            
            pdf.cell(20, 8, data_reg, border=1, align="C")
            
            if tipo_reg == "Saída":
                pdf.set_text_color(227, 6, 19) # Vermelho se for saída
                total_saidas += valor_reg
            else:
                pdf.set_text_color(0, 0, 0)
                total_entradas += valor_reg
                
            pdf.cell(20, 8, tipo_reg, border=1, align="C")
            pdf.cell(40, 8, str(cat_reg)[:18], border=1, align="L")
            pdf.cell(85, 8, str(desc_reg)[:40], border=1, align="L")
            pdf.cell(25, 8, f"R$ {valor_reg:.2f}", border=1, align="R")
            pdf.ln()
            pdf.set_text_color(0, 0, 0)

        liquido = total_entradas - total_saidas

        # Resumo de Valores
        pdf.ln(5)
        pdf.set_font("Arial", 'B', 11)
        pdf.cell(0, 7, f"Total de Entradas: R$ {total_entradas:.2f}", ln=True)
        pdf.set_text_color(227, 6, 19)
        pdf.cell(0, 7, f"Total de Saídas: R$ {total_saidas:.2f}", ln=True)
        
        pdf.set_text_color(0, 0, 0)
        if liquido >= 0:
            pdf.set_text_color(0, 100, 0) # Verde se positivo
            
        pdf.cell(0, 9, f"Saldo Líquido Acumulado: R$ {liquido:.2f}", ln=True)

        nome_arquivo = f"Relatorio_Geral_{datetime.now().strftime('%d%m%Y_%H%M')}.pdf"
        pdf.output(nome_arquivo)
        messagebox.showinfo("Sucesso", f"Relatório exportado com sucesso:\n{nome_arquivo}")

if __name__ == "__main__":
    inicializar_banco()
    root = tk.Tk()
    app = AppFinanceiro(root)
    root.mainloop()
