import streamlit as st
import psycopg
import pandas as pd
from datetime import datetime, date
from PIL import Image
from fpdf import FPDF
import io
import plotly.express as px
import streamlit.components.v1 as components

# =========================================
# TENTAR IMPORTAR PYZBAR (LEITOR DE BARRAS)
# =========================================
try:
    from pyzbar.pyzbar import decode
    BARCODE_ENABLED = True
except Exception:
    # No Streamlit Cloud, geralmente cai aqui
    BARCODE_ENABLED = False

# =========================================
# CONFIGURAÇÃO GLOBAL DA PÁGINA
# =========================================
st.set_page_config(
    page_title="Controle de Validade",
    page_icon="📦",
    layout="wide",
)

def aplicar_estilo_profissional():
    # CSS básico (ainda ajuda a limpar o layout)
    st.markdown("""
    <style>
    #MainMenu {visibility: hidden !important;}
    footer {visibility: hidden !important;}
    header {visibility: hidden !important;}
    [data-testid="stHeader"] {display: none !important;}
    [data-testid="stToolbar"] {display: none !important;}
    [data-testid="stDecoration"] {display: none !important;}

    .block-container {
        padding-top: 1rem;
        padding-bottom: 1rem;
        padding-left: 1rem;
        padding-right: 1rem;
    }
    </style>
    """, unsafe_allow_html=True)

    # JS agressivo: esconde qualquer coisa que tenha "streamlit" ou "github" no texto
    components.html("""
    <script>
    function hideStreamlitBadges() {
      const keywords = ['made with streamlit', 'streamlit', 'view source on github', 'github'];
      const nodes = document.querySelectorAll('a, div, span, footer, button');

      nodes.forEach(el => {
        const txt = (el.innerText || '').toLowerCase().trim();
        if (!txt) return;
        if (keywords.some(k => txt.includes(k))) {
          el.style.display = 'none';
        }
      });
    }

    // roda agora
    hideStreamlitBadges();
    // e continua rodando a cada 1,5s (caso o Streamlit recrie o footer)
    setInterval(hideStreamlitBadges, 1500);
    </script>
    """, height=0)




# =========================================
# CONEXÃO COM SUPABASE POSTGRES
# =========================================
def get_conn():
    cfg = st.secrets["postgres"]
    return psycopg.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["database"],
        user=cfg["user"],
        password=cfg["password"],
        sslmode=cfg["sslmode"],
    )

# =========================================
# LOGIN (USUÁRIO NO BANCO)
# =========================================
def validate_login(username, password):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, username
        FROM validate_user(%s, %s)
        """,
        (username, password),
    )

    result = cur.fetchone()
    cur.close()
    conn.close()
    return result


def pagina_login():
    st.title("🔐 Login")

    with st.form("login_form"):
        username = st.text_input("Usuário")
        password = st.text_input("Senha", type="password")
        ok = st.form_submit_button("Entrar")

    if ok:
        user = validate_login(username, password)
        if user:
            st.session_state["logged"] = True
            st.session_state["user_id"] = user[0]
            st.session_state["username"] = user[1]

            # ➜ ir automaticamente para Cadastro
            st.session_state["page"] = "Cadastro"
            st.success(f"Bem-vindo, {user[1]}!")

            st.rerun()
        else:
            st.error("Usuário ou senha incorretos.")


def exigir_login():
    if not st.session_state.get("logged", False):
        pagina_login()
        st.stop()

# =========================================
# CRUD DE PRODUTOS E MOVIMENTOS
# =========================================
def insert_product(ean, batch, expiry, quantity):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO products (ean, batch, expiry, quantity)
        VALUES (%s, %s, %s, %s)
        RETURNING id;
        """,
        (ean, batch, expiry, quantity),
    )

    product_id = cur.fetchone()[0]

    # movimento de entrada
    cur.execute(
        """
        INSERT INTO movements (product_id, movement_type, quantity)
        VALUES (%s, 'in', %s)
        """,
        (product_id, quantity),
    )

    conn.commit()
    cur.close()
    conn.close()
    return product_id


def get_products():
    conn = get_conn()
    df = pd.read_sql("SELECT * FROM products ORDER BY expiry ASC", conn)
    conn.close()
    return df


def update_product_quantity(product_id, new_qty, movement_type=None, diff_qty=0):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE products
        SET quantity = %s
        WHERE id = %s
        """,
        (new_qty, product_id),
    )

    if movement_type and diff_qty > 0:
        cur.execute(
            """
            INSERT INTO movements (product_id, movement_type, quantity)
            VALUES (%s, %s, %s)
            """,
            (product_id, movement_type, diff_qty),
        )

    conn.commit()
    cur.close()
    conn.close()


def get_movements():
    conn = get_conn()
    df = pd.read_sql("SELECT * FROM movements", conn)
    conn.close()
    return df


def calc_summary():
    products = get_products()
    movements = get_movements()

    # Estoque atual = soma das quantidades na tabela de produtos
    total_stock = 0
    if not products.empty:
        total_stock = int(products["quantity"].sum())

    # Vendas
    total_sales = 0
    if not movements.empty:
        total_sales = int(
            movements.loc[movements["movement_type"] == "sale", "quantity"].sum()
        )

    # -------- VENCIDOS --------
    expired_by_movements = 0  # já baixados como vencidos
    expired_in_stock = 0  # ainda no estoque, mas com data vencida

    if not movements.empty:
        expired_by_movements = int(
            movements.loc[movements["movement_type"] == "expired", "quantity"].sum()
        )

    if not products.empty:
        products = products.copy()
        products["expiry_date"] = pd.to_datetime(products["expiry"]).dt.date
        hoje = date.today()
        expired_in_stock = int(
            products.loc[products["expiry_date"] < hoje, "quantity"].sum()
        )

    # total vencido = em estoque + já descartado
    total_expired = expired_by_movements + expired_in_stock

    return total_stock, total_sales, total_expired

# =========================================
# LEITURA DE CÓDIGO DE BARRAS (OPCIONAL)
# =========================================
def read_barcode_from_image(image_file):
    # No Streamlit Cloud, BARCODE_ENABLED será False → sempre retorna None
    if not BARCODE_ENABLED:
        return None

    img = Image.open(image_file)
    decoded = decode(img)
    if decoded:
        return decoded[0].data.decode("utf-8")
    return None


# =========================================
# GERAR PDF
# =========================================
def gerar_pdf_relatorio(df_produtos, total_stock, total_sales, total_expired):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "Relatório de Validade de Produtos", ln=True, align="C")

    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 8, f"Gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')}", ln=True)
    pdf.ln(5)

    # Resumo
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Resumo Geral:", ln=True)
    pdf.set_font("Arial", "", 11)
    pdf.cell(0, 6, f"Estoque total: {total_stock}", ln=True)
    pdf.cell(0, 6, f"Quantidade vendida: {total_sales}", ln=True)
    pdf.cell(0, 6, f"Quantidade vencida: {total_expired}", ln=True)

    pdf.ln(8)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, "Itens detalhados:", ln=True)

    # Cabeçalho da tabela
    pdf.set_font("Arial", "B", 9)
    pdf.cell(30, 6, "EAN", border=1)
    pdf.cell(25, 6, "Lote", border=1)
    pdf.cell(25, 6, "Validade", border=1)
    pdf.cell(20, 6, "Estoque", border=1)
    pdf.cell(25, 6, "Vendida", border=1)
    pdf.cell(25, 6, "Vencida", border=1)
    pdf.ln(6)

    # Linhas da tabela
    pdf.set_font("Arial", "", 9)
    for _, row in df_produtos.iterrows():
        expiry = pd.to_datetime(row["expiry"]).strftime("%d/%m/%Y")

        estoque_atual = int(row.get("quantity", 0))
        qtd_vendida = int(row.get("sale", 0))
        qtd_vencida = int(row.get("expired", 0))

        pdf.cell(30, 6, str(row["ean"]), border=1)
        pdf.cell(25, 6, str(row["batch"]), border=1)
        pdf.cell(25, 6, expiry, border=1)
        pdf.cell(20, 6, str(estoque_atual), border=1)
        pdf.cell(25, 6, str(qtd_vendida), border=1)
        pdf.cell(25, 6, str(qtd_vencida), border=1)
        pdf.ln(6)

    return bytes(pdf.output(dest="S"))


# =========================================
# PÁGINA: CADASTRO
# =========================================
def pagina_cadastro():
    exigir_login()
    st.title("📦 Cadastro de Produtos")

    # estados para câmera / EAN lido
    if "show_camera" not in st.session_state:
        st.session_state["show_camera"] = False
    if "ean_scanned" not in st.session_state:
        st.session_state["ean_scanned"] = ""

    scanned = None
    camera_file = None

    # =======================
    # Botão para abrir / fechar câmera
    # =======================
    if BARCODE_ENABLED:
        if st.session_state["show_camera"]:
            st.info("Aponte a câmera para o código de barras e tire a foto.")

            camera_file = st.camera_input("Escanear EAN", key="camera_ean")

            if camera_file:
                scanned = read_barcode_from_image(camera_file)
                if scanned:
                    st.session_state["ean_scanned"] = scanned
                    st.success(f"EAN lido: {scanned}")
                else:
                    st.warning("Não foi possível ler o código de barras. Tente novamente.")

            if st.button("Fechar câmera"):
                st.session_state["show_camera"] = False
                st.rerun()
        else:
            if st.button("📷 Ler EAN com câmera"):
                st.session_state["show_camera"] = True
                st.rerun()
    else:
        # Ambiente onde pyzbar não funciona (ex.: Streamlit Cloud)
        st.info(
            "🔎 Leitura de código de barras via câmera não está disponível neste servidor.\n\n"
            "Use o campo de EAN abaixo para digitar o código (no celular o teclado numérico facilita)."
        )

    # =======================
    # Formulário de cadastro
    # =======================
    with st.form("cad"):
        ean = st.text_input(
            "EAN",
            value=st.session_state.get("ean_scanned", ""),
            help="Digite ou cole o código de barras.",
        )
        lote = st.text_input("Lote")
        validade = st.date_input("Validade", value=date.today())
        quantidade = st.number_input("Quantidade", min_value=1, step=1)
        ok = st.form_submit_button("Salvar")

    if ok:
        insert_product(ean, lote, validade, int(quantidade))
        st.success("Produto salvo com sucesso!")
        # limpa EAN para próximo cadastro
        st.session_state["ean_scanned"] = ""


# =========================================
# PÁGINA: CONTROLE DE ESTOQUE
# =========================================
def pagina_estoque():
    exigir_login()

    st.title("📊 Controle de Estoque")

    # ⚠️ Verificar produtos vencidos
    df_check = get_products()

    if not df_check.empty:
        df_check["expiry_date"] = pd.to_datetime(df_check["expiry"]).dt.date
        hoje = date.today()

        vencidos = df_check[
            (df_check["expiry_date"] < hoje) & (df_check["quantity"] > 0)
        ]

        if not vencidos.empty:
            st.error(
                f"⚠️ Atenção: Existem **{len(vencidos)}** produtos vencidos ainda no estoque!"
            )

    df = get_products()
    if df.empty:
        st.info("Nenhum produto cadastrado.")
        return

    # montar descrição bonita
    df["ean"] = df["ean"].astype(str)
    df["batch"] = df["batch"].astype(str)
    df["expiry_str"] = pd.to_datetime(df["expiry"]).dt.strftime("%d/%m/%Y")
    df["desc"] = df["ean"] + " | Lote " + df["batch"] + " | Val " + df["expiry_str"]

    escolha = st.selectbox("Escolha o item", df["desc"])

    p = df[df["desc"] == escolha].iloc[0]
    prod_id = int(p["id"])
    estoque_atual = int(p["quantity"])

    st.metric("Estoque atual", estoque_atual)

    nova = st.number_input("Nova quantidade", min_value=0, value=estoque_atual)
    confirmar = st.button("Atualizar")

    # Passo 1: clicar em Atualizar define o que está "pendente"
    if confirmar:
        nova = int(nova)
        if nova == estoque_atual:
            st.info("Nada mudou.")
            st.session_state["show_modal"] = False
            st.session_state["pending_update"] = None
        elif nova > estoque_atual:
            # aumento de estoque (entrada simples)
            diff = nova - estoque_atual
            update_product_quantity(prod_id, nova, "in", diff)
            st.success(f"Entrada registrada (+{diff}).")
            st.session_state["show_modal"] = False
            st.session_state["pending_update"] = None
        else:
            # diminuição → guarda info e mostra card de confirmação
            diff = estoque_atual - nova
            st.session_state["pending_update"] = {
                "product_id": prod_id,
                "old": estoque_atual,
                "new": nova,
                "diff": diff,
            }
            st.session_state["show_modal"] = True

    # Passo 2: card de confirmação de baixa
    if st.session_state.get("show_modal") and st.session_state.get("pending_update"):
        pending = st.session_state["pending_update"]

        st.markdown("---")
        st.markdown("### ⚠️ Confirmar baixa de estoque")

        st.write(f"**De:** {pending['old']}")
        st.write(f"**Para:** {pending['new']}")
        st.write(f"**Quantidade a baixar:** {pending['diff']}")

        motivo = st.radio(
            "Essa baixa foi por:",
            ["Venda", "Vencido / Descarte", "Outro ajuste"],
            index=0,
            key="motivo_baixa",
        )

        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ Confirmar baixa", key="btn_confirma_baixa"):
                movement_type = "sale"
                if motivo == "Vencido / Descarte":
                    movement_type = "expired"
                elif motivo == "Outro ajuste":
                    movement_type = "adjust"

                update_product_quantity(
                    pending["product_id"],
                    pending["new"],
                    movement_type,
                    pending["diff"],
                )

                st.success(
                    f"Baixa de {pending['diff']} unidades registrada como **{motivo}**."
                )
                st.session_state["show_modal"] = False
                st.session_state["pending_update"] = None
                st.rerun()

        with col2:
            if st.button("❌ Cancelar", key="btn_cancela_baixa"):
                st.session_state["show_modal"] = False
                st.session_state["pending_update"] = None
                st.info("Baixa cancelada.")
                st.rerun()


# =========================================
# PÁGINA: RELATÓRIOS
# =========================================
def pagina_relatorios():
    exigir_login()
    st.title("📈 Relatórios")

    # Produtos e movimentos
    df_prod = get_products()
    total_stock, total_sales, total_expired = calc_summary()

    if df_prod.empty:
        st.info("Nenhum produto cadastrado ainda.")
        return

    df_mov = get_movements()

    # ===============================
    # Montar DF consolidado por produto
    # ===============================
    if not df_mov.empty:
        mov_agg = (
            df_mov.groupby(["product_id", "movement_type"])["quantity"]
            .sum()
            .unstack(fill_value=0)
            .reset_index()
        )
    else:
        mov_agg = pd.DataFrame(
            columns=["product_id", "sale", "expired", "in", "adjust"]
        )

    df_rel = df_prod.merge(
        mov_agg, left_on="id", right_on="product_id", how="left"
    )

    # Garantir colunas de movimento
    for col in ["sale", "expired", "in", "adjust"]:
        if col not in df_rel.columns:
            df_rel[col] = 0

    df_rel[["sale", "expired", "in", "adjust"]] = (
        df_rel[["sale", "expired", "in", "adjust"]].fillna(0).astype(int)
    )
    # -----------------------------
    # CALCULAR VENCIDOS AUTOMÁTICOS
    # -----------------------------
    df_rel["expiry_date"] = pd.to_datetime(df_rel["expiry"]).dt.date
    hoje = date.today()

    df_rel["expired_auto"] = df_rel.apply(
        lambda row: row["quantity"] if row["expiry_date"] < hoje else 0,
        axis=1,
    )

    # -----------------------------
    # VENCIDO TOTAL = movimento + automático
    # -----------------------------
    df_rel["expired_total"] = df_rel["expired"] + df_rel["expired_auto"]

    # ===============================
    # Métricas gerais
    # ===============================
    col1, col2, col3 = st.columns(3)
    col1.metric("Estoque atual", total_stock)
    col2.metric("Quantidade vendida", total_sales)
    col3.metric("Quantidade vencida", total_expired)

    # ===============================
    # Gráfico de pizza (donut)
    # ===============================
    df_graf = pd.DataFrame(
        {
            "Categoria": ["Estoque", "Vendas", "Vencidos"],
            "Quantidade": [total_stock, total_sales, total_expired],
        }
    )

    fig = px.pie(
        df_graf,
        values="Quantidade",
        names="Categoria",
        title="Distribuição Geral de Quantidades",
        color="Categoria",
        color_discrete_map={
            "Estoque": "#1f77b4",
            "Vendas": "#2ca02c",
            "Vencidos": "#d62728",
        },
        hole=0.35,
    )

    fig.update_traces(
        textinfo="percent+label",
        pull=[0.02, 0.02, 0.08],
        marker=dict(line=dict(color="white", width=2)),
    )

    fig.update_layout(
        showlegend=True,
        legend_title_text="Categorias",
        title_x=0.5,
        font=dict(size=14),
    )

    st.plotly_chart(fig, use_container_width=True)

    # ===============================
    # Tabela detalhada
    # ===============================
    st.markdown("### 📋 Detalhamento por produto")
    df_tela_view = df_rel[
        [
            "ean",
            "batch",
            "expiry",
            "quantity",
            "sale",
            "expired",
            "expired_auto",
            "expired_total",
        ]
    ]

    df_tela_view.columns = [
        "EAN",
        "Lote",
        "Validade",
        "Em estoque",
        "Vendida",
        "Vencida (registrada)",
        "Vencida em estoque",
        "Vencida (total)",
    ]

    df_tela_view["Validade"] = pd.to_datetime(
        df_tela_view["Validade"]
    ).dt.strftime("%d/%m/%Y")

    st.dataframe(df_tela_view, use_container_width=True)

    # ===============================
    # Exportações
    # ===============================
    st.subheader("Exportar:")

    # Excel em português
    excel_buffer = io.BytesIO()
    df_export = df_rel.copy()

    # remover timezone de qualquer coluna datetime com tz
    for col in df_export.select_dtypes(include=["datetimetz"]).columns:
        df_export[col] = df_export[col].dt.tz_localize(None)

    df_export["Validade"] = pd.to_datetime(df_export["expiry"]).dt.strftime(
        "%d/%m/%Y"
    )

    df_excel = df_rel[
        [
            "ean",
            "batch",
            "expiry",
            "quantity",
            "sale",
            "expired",
            "expired_auto",
            "expired_total",
        ]
    ].rename(
        columns={
            "ean": "EAN",
            "batch": "Lote",
            "expiry": "Validade",
            "quantity": "Qtde em estoque",
            "sale": "Qtde vendida",
            "expired": "Qtde vencida (registrada)",
            "expired_auto": "Qtde vencida em estoque",
            "expired_total": "Qtde vencida (total)",
        }
    )

    df_excel["Validade"] = pd.to_datetime(
        df_excel["Validade"]
    ).dt.strftime("%d/%m/%Y")

    df_excel.to_excel(excel_buffer, index=False, sheet_name="Relatório")
    excel_buffer.seek(0)

    st.download_button(
        "📥 Baixar Excel (pt-BR)",
        excel_buffer.getvalue(),
        "relatorio_validade.xlsx",
    )

    # PDF
    pdf_bytes = gerar_pdf_relatorio(df_rel, total_stock, total_sales, total_expired)
    st.download_button("📄 Baixar PDF", pdf_bytes, "relatorio_validade.pdf")


# =========================================
# MENU / MAIN
# =========================================
def main():
    aplicar_estilo_profissional()

    # Estados iniciais
    if "logged" not in st.session_state:
        st.session_state["logged"] = False
    if "pending_update" not in st.session_state:
        st.session_state["pending_update"] = None
    if "show_modal" not in st.session_state:
        st.session_state["show_modal"] = False
    if "page" not in st.session_state:
        st.session_state["page"] = "Cadastro"

    # Se não estiver logado, mostra só a tela de login (sem menu)
    if not st.session_state["logged"]:
        pagina_login()
        return

    # =========================
    # BARRA SUPERIOR (TÍTULO + USUÁRIO)
    # =========================
    with st.container():
        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown(
                "<h2 style='margin-bottom: 0.2rem;'>📦 Controle de Validade</h2>",
                unsafe_allow_html=True,
            )
        with col2:
            st.markdown(
                f"<p style='text-align: right; margin-top: 0.6rem;'>👤 <b>{st.session_state['username']}</b></p>",
                unsafe_allow_html=True,
            )

    # =========================
    # MENU SUPERIOR (RADIO HORIZONTAL)
    # =========================
    paginas = ["Cadastro", "Estoque", "Relatórios"]
    page = st.radio(
        "Navegação",
        paginas,
        horizontal=True,
        index=paginas.index(st.session_state.get("page", "Cadastro")),
    )

    st.session_state["page"] = page

    st.markdown("---")

    # Roteamento
    if page == "Cadastro":
        pagina_cadastro()
    elif page == "Estoque":
        pagina_estoque()
    else:
        pagina_relatorios()


if __name__ == "__main__":
    main()