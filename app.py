import streamlit as st
import sqlite3
from datetime import datetime, date
import pandas as pd
from PIL import Image
from pyzbar.pyzbar import decode
from fpdf import FPDF
import io
import os

# =========================================
# CONFIGURAÇÃO BÁSICA
# =========================================
st.set_page_config(
    page_title="Controle de Validade",
    layout="wide",
    page_icon="🧊"
)

# Credenciais simples (pode depois buscar do banco ou Supabase)
USUARIO_PADRAO = "admin"
SENHA_PADRAO = "1234"


# =========================================
# FUNÇÕES DE BANCO (SQLite)
# =========================================
def get_conn():
    # Cria / abre o banco local
    conn = sqlite3.connect("validade.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ean TEXT NOT NULL,
            batch TEXT NOT NULL,
            expiry DATE NOT NULL,
            quantity INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            movement_type TEXT NOT NULL,     -- 'in', 'sale', 'adjust', 'expired'
            quantity INTEGER NOT NULL,       -- sempre positivo
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
    """)
    conn.commit()
    conn.close()

def insert_product(ean, batch, expiry, quantity):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO products (ean, batch, expiry, quantity)
        VALUES (?, ?, ?, ?)
    """, (ean, batch, expiry, quantity))
    product_id = cur.lastrowid

    # movimento de entrada
    cur.execute("""
        INSERT INTO movements (product_id, movement_type, quantity)
        VALUES (?, 'in', ?)
    """, (product_id, quantity))

    conn.commit()
    conn.close()
    return product_id

def get_products():
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM products ORDER BY expiry ASC;", conn)
    conn.close()
    return df

def update_product_quantity(product_id, new_qty, movement_type=None, diff_qty=0):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("UPDATE products SET quantity = ? WHERE id = ?", (new_qty, product_id))

    if movement_type and diff_qty > 0:
        cur.execute("""
            INSERT INTO movements (product_id, movement_type, quantity)
            VALUES (?, ?, ?)
        """, (product_id, movement_type, diff_qty))

    conn.commit()
    conn.close()

def get_movements():
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM movements;", conn)
    conn.close()
    return df

def calc_summary():
    products = get_products()
    movements = get_movements()

    today = date.today()

    # Estoque total atual (independente de vencimento)
    total_stock = int(products["quantity"].sum()) if not products.empty else 0

    # Vencidos = quantidade ainda em estoque com data < hoje
    if not products.empty:
        products["expiry_date"] = pd.to_datetime(products["expiry"]).dt.date
        total_expired = int(products.loc[products["expiry_date"] < today, "quantity"].sum())
    else:
        total_expired = 0

    # Vendas = soma dos movimentos type 'sale'
    if not movements.empty:
        total_sales = int(movements.loc[movements["movement_type"] == "sale", "quantity"].sum())
    else:
        total_sales = 0

    return total_stock, total_sales, total_expired


# =========================================
# CÂMERA / LEITURA DE EAN
# =========================================
def read_barcode_from_image(image_file):
    """
    Recebe um arquivo (st.camera_input / st.file_uploader) e tenta ler o código de barras.
    Retorna string do EAN ou None.
    """
    img = Image.open(image_file)
    decoded = decode(img)
    if decoded:
        # pega o primeiro código encontrado
        return decoded[0].data.decode("utf-8")
    return None


# =========================================
# PDF DO RELATÓRIO
# =========================================
def gerar_pdf_relatorio(df_produtos, total_stock, total_sales, total_expired):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "Relatório de Validade de Produtos", ln=True, align="C")

    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 8, f"Data de geração: {datetime.now().strftime('%d/%m/%Y %H:%M')}", ln=True)

    pdf.ln(4)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Resumo:", ln=True)
    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 6, f"Estoque atual: {total_stock}", ln=True)
    pdf.cell(0, 6, f"Quantidade vendida: {total_sales}", ln=True)
    pdf.cell(0, 6, f"Quantidade vencida (em estoque): {total_expired}", ln=True)

    pdf.ln(6)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Detalhamento por produto:", ln=True)

    pdf.set_font("Arial", "B", 10)
    pdf.cell(35, 6, "EAN", border=1)
    pdf.cell(25, 6, "Lote", border=1)
    pdf.cell(30, 6, "Validade", border=1)
    pdf.cell(20, 6, "Qtde", border=1)
    pdf.cell(20, 6, "Vencido?", border=1)
    pdf.ln(6)

    pdf.set_font("Arial", "", 10)
    today = date.today()
    for _, row in df_produtos.iterrows():
        expiry_date = datetime.strptime(row["expiry"], "%Y-%m-%d").date()
        vencido = "Sim" if expiry_date < today and row["quantity"] > 0 else "Não"

        pdf.cell(35, 6, str(row["ean"])[:15], border=1)
        pdf.cell(25, 6, str(row["batch"])[:10], border=1)
        pdf.cell(30, 6, expiry_date.strftime("%d/%m/%Y"), border=1)
        pdf.cell(20, 6, str(row["quantity"]), border=1)
        pdf.cell(20, 6, vencido, border=1)
        pdf.ln(6)

    # Exporta PDF em bytes
    pdf_bytes = pdf.output(dest="S").encode("latin-1")
    return pdf_bytes


# =========================================
# LOGIN
# =========================================
def pagina_login():
    st.title("🔐 Login - Controle de Validade")

    with st.form("login_form"):
        usuario = st.text_input("Usuário")
        senha = st.text_input("Senha", type="password")
        submit = st.form_submit_button("Entrar")

    if submit:
        if usuario == USUARIO_PADRAO and senha == SENHA_PADRAO:
            st.session_state["logged"] = True
            st.success("Login realizado com sucesso!")
        else:
            st.error("Usuário ou senha inválidos.")


def exigir_login():
    if not st.session_state.get("logged", False):
        st.warning("Você precisa estar logado para acessar esta página.")
        pagina_login()
        st.stop()


# =========================================
# PÁGINA 2: CADASTRO DE PRODUTOS
# =========================================
def pagina_cadastro():
    exigir_login()
    st.title("📦 Cadastro de Produtos Perecíveis")

    st.markdown("#### Leitor de EAN com câmera (opcional)")
    camera_file = st.camera_input("Aponte a câmera para o código de barras")

    scanned_ean = None
    if camera_file is not None:
        scanned_ean = read_barcode_from_image(camera_file)
        if scanned_ean:
            st.success(f"EAN lido: **{scanned_ean}**")
        else:
            st.warning("Não foi possível identificar o código de barras na imagem.")

    st.markdown("---")
    st.markdown("#### Formulário de cadastro")

    with st.form("form_cadastro"):
        ean = st.text_input("EAN", value=scanned_ean if scanned_ean else "")
        lote = st.text_input("Lote")
        validade = st.date_input("Data de validade", value=date.today())
        quantidade = st.number_input("Quantidade", min_value=0, value=0, step=1)

        submitted = st.form_submit_button("Salvar produto")

    if submitted:
        if not ean or not lote or quantidade <= 0:
            st.error("Preencha EAN, Lote e uma quantidade maior que zero.")
        else:
            insert_product(
                ean=ean,
                batch=lote,
                expiry=validade.isoformat(),
                quantity=int(quantidade)
            )
            st.success("Produto cadastrado com sucesso!")


# =========================================
# PÁGINA 3: CONTROLE DE ESTOQUE
# =========================================
def pagina_estoque():
    exigir_login()
    st.title("📊 Controle de Estoque")

    df = get_products()
    if df.empty:
        st.info("Nenhum produto cadastrado ainda.")
        return

    st.write("Selecione um produto para editar a quantidade:")

    df["descricao"] = df["ean"] + " | Lote " + df["batch"] + " | Validade " + df["expiry"]
    escolha = st.selectbox("Produto", options=df["descricao"].tolist())

    produto = df[df["descricao"] == escolha].iloc[0]
    prod_id = int(produto["id"])
    estoque_atual = int(produto["quantity"])

    col1, col2 = st.columns(2)
    with col1:
        st.metric("Estoque atual", estoque_atual)
        st.write(f"EAN: **{produto['ean']}**")
        st.write(f"Lote: **{produto['batch']}**")
        st.write(f"Validade: **{produto['expiry']}**")

    with col2:
        nova_qtde = st.number_input(
            "Nova quantidade em estoque",
            min_value=0,
            value=estoque_atual,
            step=1
        )
        confirmar = st.button("Atualizar estoque")

    # Guardar info no session_state para usar no modal
    if "pending_update" not in st.session_state:
        st.session_state["pending_update"] = None
    if "show_modal" not in st.session_state:
        st.session_state["show_modal"] = False

    if confirmar:
        nova_qtde = int(nova_qtde)
        if nova_qtde == estoque_atual:
            st.info("Quantidade não foi alterada.")
        elif nova_qtde > estoque_atual:
            # aumento de estoque (entrada simples)
            diff = nova_qtde - estoque_atual
            update_product_quantity(prod_id, nova_qtde, movement_type="in", diff_qty=diff)
            st.success(f"Estoque aumentado em {diff} unidades (entrada).")
        else:
            # diminuição: abrir modal para saber se é venda
            diff = estoque_atual - nova_qtde
            st.session_state["pending_update"] = {
                "product_id": prod_id,
                "old": estoque_atual,
                "new": nova_qtde,
                "diff": diff
            }
            st.session_state["show_modal"] = True

    # Modal de confirmação de baixa
    if st.session_state.get("show_modal") and st.session_state.get("pending_update"):
        pending = st.session_state["pending_update"]
        with st.modal("Confirmar baixa de estoque"):
            st.write(f"Você está reduzindo o estoque de **{pending['old']}** para **{pending['new']}**.")
            st.write(f"Quantidade a baixar: **{pending['diff']}**")

            motivo = st.radio(
                "Essa baixa foi por:",
                options=["Venda", "Vencido / Descarte", "Outro ajuste"],
                index=0
            )

            col_a, col_b = st.columns(2)
            with col_a:
                confirmar_modal = st.button("Confirmar baixa")
            with col_b:
                cancelar_modal = st.button("Cancelar")

            if confirmar_modal:
                movement_type = "sale"
                if motivo == "Vencido / Descarte":
                    movement_type = "expired"
                elif motivo == "Outro ajuste":
                    movement_type = "adjust"

                update_product_quantity(
                    product_id=pending["product_id"],
                    new_qty=pending["new"],
                    movement_type=movement_type,
                    diff_qty=pending["diff"]
                )
                st.success(f"Baixa de {pending['diff']} unidades registrada como '{motivo}'.")
                st.session_state["show_modal"] = False
                st.session_state["pending_update"] = None

            if cancelar_modal:
                st.session_state["show_modal"] = False
                st.session_state["pending_update"] = None


# =========================================
# PÁGINA 4: RELATÓRIOS
# =========================================
def pagina_relatorios():
    exigir_login()
    st.title("📈 Relatórios de Estoque, Vendas e Vencidos")

    df_produtos = get_products()
    total_stock, total_sales, total_expired = calc_summary()

    col1, col2, col3 = st.columns(3)
    col1.metric("Estoque atual", total_stock)
    col2.metric("Quantidade vendida (acumulado)", total_sales)
    col3.metric("Quantidade vencida em estoque", total_expired)

    st.markdown("---")
    st.subheader("Gráfico geral")

    df_chart = pd.DataFrame({
        "Categoria": ["Estoque atual", "Vendas", "Vencidos"],
        "Quantidade": [total_stock, total_sales, total_expired]
    })
    st.bar_chart(df_chart, x="Categoria", y="Quantidade")

    st.markdown("---")
    st.subheader("Tabela detalhada")

    if df_produtos.empty:
        st.info("Nenhum produto cadastrado.")
        return

    # Ajuste de colunas para exibição
    df_exibir = df_produtos.copy()
    df_exibir["expiry"] = pd.to_datetime(df_exibir["expiry"]).dt.strftime("%d/%m/%Y")
    st.dataframe(df_exibir[["ean", "batch", "expiry", "quantity"]], use_container_width=True)

    # ====== EXPORTAÇÕES ======
    st.markdown("### Exportar relatório")

    # Excel
    buffer_excel = io.BytesIO()
    df_export = df_produtos.copy()
    with pd.ExcelWriter(buffer_excel, engine="openpyxl") as writer:
        df_export.to_excel(writer, index=False, sheet_name="Relatorio")
    buffer_excel.seek(0)

    st.download_button(
        label="📥 Baixar Excel",
        data=buffer_excel,
        file_name="relatorio_validade.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    # PDF
    pdf_bytes = gerar_pdf_relatorio(df_produtos, total_stock, total_sales, total_expired)
    st.download_button(
        label="📄 Baixar PDF",
        data=pdf_bytes,
        file_name="relatorio_validade.pdf",
        mime="application/pdf"
    )


# =========================================
# MAIN / NAVEGAÇÃO
# =========================================
def main():
    init_db()

    if "logged" not in st.session_state:
        st.session_state["logged"] = False

    st.sidebar.title("Menu")
    page = st.sidebar.radio(
        "Navegação",
        options=["Login", "Cadastro de Produtos", "Controle de Estoque", "Relatórios"]
    )

    if page == "Login":
        pagina_login()
    elif page == "Cadastro de Produtos":
        pagina_cadastro()
    elif page == "Controle de Estoque":
        pagina_estoque()
    elif page == "Relatórios":
        pagina_relatorios()


if __name__ == "__main__":
    main()
