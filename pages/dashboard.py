import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import io

st.set_page_config(
    page_title="XBRL Data Explorer",
    page_icon="📊",
    layout="wide",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .metric-card {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 1rem 1.25rem;
        margin-bottom: 0.5rem;
    }
    .metric-value {
        font-size: 1.8rem;
        font-weight: 700;
        color: #1e3a5f;
        line-height: 1.1;
    }
    .metric-label {
        font-size: 0.8rem;
        color: #64748b;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-top: 0.2rem;
    }
    .section-header {
        font-size: 1.1rem;
        font-weight: 600;
        color: #1e3a5f;
        margin: 1.5rem 0 0.75rem 0;
        padding-bottom: 0.4rem;
        border-bottom: 2px solid #e2e8f0;
    }
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────
st.title("📊 XBRL Data Explorer")
st.markdown("Upload a CSV produced by the converter to explore the data interactively.")

# ── File upload ───────────────────────────────────────────────────────────────
uploaded = st.file_uploader(
    "Upload converted CSV",
    type=["csv"],
    help="Use the converter on the main page to produce this file from a Companies House ZIP."
)

if uploaded is None:
    st.info("Upload a CSV file above to get started. Use the **Companies House XBRL → CSV** page to generate one.", icon="👈")
    st.stop()

# ── Load data ─────────────────────────────────────────────────────────────────
@st.cache_data
def load(file):
    df = pd.read_csv(file, low_memory=False)
    # Parse dates where possible
    for col in ["balance_sheet_date", "period_start", "period_end", "filed_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df

df = load(uploaded)

# ── Sidebar filters ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filters")

    # Accounts type
    if "accounts_type" in df.columns:
        types = sorted(df["accounts_type"].dropna().unique().tolist())
        selected_types = st.multiselect("Accounts type", types, default=types)
        df = df[df["accounts_type"].isin(selected_types)] if selected_types else df

    # Date range
    if "balance_sheet_date" in df.columns and df["balance_sheet_date"].notna().any():
        min_date = df["balance_sheet_date"].min().date()
        max_date = df["balance_sheet_date"].max().date()
        date_range = st.date_input(
            "Balance sheet date range",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
        )
        if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
            df = df[
                (df["balance_sheet_date"] >= pd.Timestamp(date_range[0])) &
                (df["balance_sheet_date"] <= pd.Timestamp(date_range[1]))
            ]

    # Only show companies with turnover
    if "turnover" in df.columns:
        only_with_turnover = st.checkbox("Only companies with turnover filed")
        if only_with_turnover:
            df = df[df["turnover"].notna()]

    st.markdown("---")
    st.caption(f"**{len(df):,}** rows after filtering")

if df.empty:
    st.warning("No data matches the current filters.")
    st.stop()

# ── Key metrics ───────────────────────────────────────────────────────────────
st.markdown('<div class="section-header">Overview</div>', unsafe_allow_html=True)

def fmt_currency(val):
    if pd.isna(val): return "—"
    if abs(val) >= 1e9: return f"£{val/1e9:.1f}bn"
    if abs(val) >= 1e6: return f"£{val/1e6:.1f}m"
    if abs(val) >= 1e3: return f"£{val/1e3:.1f}k"
    return f"£{val:,.0f}"

def fmt_number(val):
    if pd.isna(val): return "—"
    if abs(val) >= 1e6: return f"{val/1e6:.1f}m"
    if abs(val) >= 1e3: return f"{val/1e3:.1f}k"
    return f"{val:,.0f}"

id_col = "company_id" if "company_id" in df.columns else df.columns[0]
n_companies = df[id_col].nunique() if id_col in df.columns else len(df)

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-value">{n_companies:,}</div>
        <div class="metric-label">Companies</div>
    </div>""", unsafe_allow_html=True)

with col2:
    if "turnover" in df.columns:
        total_turnover = df.groupby(id_col)["turnover"].max().sum() if id_col in df.columns else df["turnover"].sum()
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-value">{fmt_currency(total_turnover)}</div>
            <div class="metric-label">Total turnover filed</div>
        </div>""", unsafe_allow_html=True)

with col3:
    if "net_assets" in df.columns:
        total_net_assets = df.groupby(id_col)["net_assets"].max().sum() if id_col in df.columns else df["net_assets"].sum()
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-value">{fmt_currency(total_net_assets)}</div>
            <div class="metric-label">Total net assets</div>
        </div>""", unsafe_allow_html=True)

with col4:
    if "employees" in df.columns:
        total_employees = df.groupby(id_col)["employees"].max().sum() if id_col in df.columns else df["employees"].sum()
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-value">{fmt_number(total_employees)}</div>
            <div class="metric-label">Total employees</div>
        </div>""", unsafe_allow_html=True)

# ── Charts ────────────────────────────────────────────────────────────────────
st.markdown('<div class="section-header">Charts</div>', unsafe_allow_html=True)

chart_col1, chart_col2 = st.columns(2)

# Accounts type breakdown
with chart_col1:
    if "accounts_type" in df.columns:
        type_counts = df.groupby("accounts_type")[id_col].nunique().reset_index()
        type_counts.columns = ["Accounts type", "Companies"]
        type_counts = type_counts.sort_values("Companies", ascending=False)
        fig = px.bar(
            type_counts, x="Accounts type", y="Companies",
            title="Companies by accounts type",
            color_discrete_sequence=["#1e3a5f"],
        )
        fig.update_layout(
            plot_bgcolor="white", paper_bgcolor="white",
            margin=dict(l=0, r=0, t=40, b=0),
            title_font_size=14,
            xaxis_title=None,
        )
        st.plotly_chart(fig, use_container_width=True)

# Filings by month
with chart_col2:
    if "balance_sheet_date" in df.columns and df["balance_sheet_date"].notna().any():
        df["month"] = df["balance_sheet_date"].dt.to_period("M").astype(str)
        monthly = df.groupby("month")[id_col].nunique().reset_index()
        monthly.columns = ["Month", "Companies"]
        monthly = monthly.sort_values("Month")
        fig2 = px.line(
            monthly, x="Month", y="Companies",
            title="Companies by balance sheet month",
            color_discrete_sequence=["#1e3a5f"],
        )
        fig2.update_layout(
            plot_bgcolor="white", paper_bgcolor="white",
            margin=dict(l=0, r=0, t=40, b=0),
            title_font_size=14,
            xaxis_title=None,
        )
        fig2.update_traces(line_width=2)
        st.plotly_chart(fig2, use_container_width=True)

# Turnover distribution
if "turnover" in df.columns:
    turnover_df = df[df["turnover"].notna() & (df["turnover"] > 0)]
    if not turnover_df.empty:
        chart_col3, chart_col4 = st.columns(2)

        with chart_col3:
            # Log-scale histogram
            fig3 = px.histogram(
                turnover_df,
                x="turnover",
                nbins=50,
                title="Turnover distribution (£)",
                log_x=True,
                color_discrete_sequence=["#2e6da4"],
            )
            fig3.update_layout(
                plot_bgcolor="white", paper_bgcolor="white",
                margin=dict(l=0, r=0, t=40, b=0),
                title_font_size=14,
                xaxis_title="Turnover (log scale)",
                yaxis_title="Count",
            )
            st.plotly_chart(fig3, use_container_width=True)

        with chart_col4:
            # Turnover bands
            def band(v):
                if v < 100_000: return "Under £100k"
                if v < 1_000_000: return "£100k–£1m"
                if v < 10_000_000: return "£1m–£10m"
                if v < 100_000_000: return "£10m–£100m"
                return "Over £100m"

            band_order = ["Under £100k", "£100k–£1m", "£1m–£10m", "£10m–£100m", "Over £100m"]
            turnover_df = turnover_df.copy()
            turnover_df["band"] = turnover_df["turnover"].apply(band)
            band_counts = turnover_df.groupby("band")[id_col].nunique().reindex(band_order).fillna(0).reset_index()
            band_counts.columns = ["Turnover band", "Companies"]

            fig4 = px.bar(
                band_counts, x="Turnover band", y="Companies",
                title="Companies by turnover band",
                color_discrete_sequence=["#2e6da4"],
            )
            fig4.update_layout(
                plot_bgcolor="white", paper_bgcolor="white",
                margin=dict(l=0, r=0, t=40, b=0),
                title_font_size=14,
                xaxis_title=None,
            )
            st.plotly_chart(fig4, use_container_width=True)

# ── Top companies table ───────────────────────────────────────────────────────
st.markdown('<div class="section-header">Top companies</div>', unsafe_allow_html=True)

rank_options = [c for c in ["turnover", "net_assets", "current_assets", "employees"] if c in df.columns]

if rank_options:
    rank_col = st.selectbox("Rank by", rank_options)
    n_top = st.slider("Show top N", min_value=5, max_value=100, value=20, step=5)

    name_col = "company_name" if "company_name" in df.columns else None
    group_cols = [id_col] + ([name_col] if name_col else [])
    agg = {rank_col: "max"}
    for extra in rank_options:
        if extra != rank_col:
            agg[extra] = "max"

    top_df = (
        df.groupby(group_cols)
        .agg(agg)
        .reset_index()
        .sort_values(rank_col, ascending=False)
        .head(n_top)
    )

    # Format currency columns for display
    display_df = top_df.copy()
    for c in ["turnover", "net_assets", "current_assets"]:
        if c in display_df.columns:
            display_df[c] = display_df[c].apply(fmt_currency)
    if "employees" in display_df.columns:
        display_df["employees"] = display_df["employees"].apply(
            lambda v: f"{int(v):,}" if pd.notna(v) else "—"
        )

    st.dataframe(display_df, use_container_width=True, hide_index=True)

# ── Full searchable table ─────────────────────────────────────────────────────
st.markdown('<div class="section-header">Search all companies</div>', unsafe_allow_html=True)

search = st.text_input("Search by company name or number", placeholder="e.g. Tesco or 00000001")

search_df = df.copy()
if search.strip():
    mask = pd.Series(False, index=search_df.index)
    if name_col and name_col in search_df.columns:
        mask |= search_df[name_col].astype(str).str.contains(search, case=False, na=False)
    if id_col in search_df.columns:
        mask |= search_df[id_col].astype(str).str.contains(search, case=False, na=False)
    search_df = search_df[mask]
    st.caption(f"{len(search_df):,} rows match '{search}'")

# Show sensible subset of columns
priority_cols = [c for c in [
    id_col, name_col, "accounts_type", "balance_sheet_date",
    "turnover", "net_assets", "current_assets", "cash", "employees",
    "period_start", "period_end",
] if c and c in search_df.columns]

st.dataframe(search_df[priority_cols].head(500), use_container_width=True, hide_index=True)
if len(search_df) > 500:
    st.caption("Showing first 500 rows. Use filters or search to narrow down.")

# ── Download filtered data ────────────────────────────────────────────────────
st.markdown('<div class="section-header">Download</div>', unsafe_allow_html=True)

buf = io.StringIO()
df.to_csv(buf, index=False)
st.download_button(
    label=f"⬇️ Download filtered data ({len(df):,} rows)",
    data=buf.getvalue().encode("utf-8"),
    file_name="xbrl_filtered.csv",
    mime="text/csv",
)
