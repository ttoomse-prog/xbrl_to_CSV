import streamlit as st
import pandas as pd
import plotly.express as px
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
    .metric-sub {
        font-size: 0.8rem;
        color: #475569;
        margin-top: 0.4rem;
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

# ── Load & deduplicate ────────────────────────────────────────────────────────
@st.cache_data
def load(file):
    df = pd.read_csv(file, low_memory=False)
    for col in ["balance_sheet_date", "period_start", "period_end", "filed_date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df

df = load(uploaded)

# Column references
id_col = "company_id" if "company_id" in df.columns else df.columns[0]
name_col = next((c for c in ["entity_current_legal_name", "company_name"] if c in df.columns), None)
turnover_field = next((c for c in ["turnover_gross_operating_revenue", "turnover"] if c in df.columns), None)
employees_field = next((c for c in ["average_number_employees_during_period", "employees"] if c in df.columns), None)

# Deduplicate: one row per company per period_start
if "period_start" in df.columns:
    sort_cols = [id_col, "period_start"] + (["balance_sheet_date"] if "balance_sheet_date" in df.columns else [])
    df = (
        df.sort_values(sort_cols, ascending=False)
        .drop_duplicates(subset=[id_col, "period_start"])
        .reset_index(drop=True)
    )

# ── Sidebar filters ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filters")

    if "accounts_type" in df.columns:
        types = sorted(df["accounts_type"].dropna().unique().tolist())
        selected_types = st.multiselect("Accounts type", types, default=types)
        if selected_types:
            df = df[df["accounts_type"].isin(selected_types)]

    if "period_start" in df.columns and df["period_start"].notna().any():
        min_date = df["period_start"].min().date()
        max_date = df["period_start"].max().date()
        date_range = st.date_input(
            "Period start date range",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
        )
        if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
            df = df[
                (df["period_start"] >= pd.Timestamp(date_range[0])) &
                (df["period_start"] <= pd.Timestamp(date_range[1]))
            ]

    if "company_dormant" in df.columns:
        dormant_filter = st.radio("Dormant status", ["All", "Active only", "Dormant only"], index=0)
        if dormant_filter == "Active only":
            df = df[df["company_dormant"].isna() | (df["company_dormant"] == False)]
        elif dormant_filter == "Dormant only":
            df = df[df["company_dormant"] == True]

    if turnover_field:
        only_with_turnover = st.checkbox("Only companies with turnover filed")
        if only_with_turnover:
            df = df[df[turnover_field].notna()]

    st.markdown("---")
    st.caption(f"**{len(df):,}** rows after filtering")

if df.empty:
    st.warning("No data matches the current filters.")
    st.stop()

# ── Formatting helpers ────────────────────────────────────────────────────────
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

# ── Overview metrics ──────────────────────────────────────────────────────────
st.markdown('<div class="section-header">Overview</div>', unsafe_allow_html=True)

n_companies = df[id_col].nunique()

# Dormant/active split
dormant_sub = ""
if "company_dormant" in df.columns:
    n_dormant = int(df[df["company_dormant"] == True][id_col].nunique())
    n_active = n_companies - n_dormant
    dormant_sub = f'<div class="metric-sub">Active: <strong>{n_active:,}</strong> &nbsp;|&nbsp; Dormant: <strong>{n_dormant:,}</strong></div>'

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-value">{n_companies:,}</div>
        <div class="metric-label">Companies</div>
        {dormant_sub}
    </div>""", unsafe_allow_html=True)

with col2:
    if turnover_field:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-value">{fmt_currency(df[turnover_field].sum())}</div>
            <div class="metric-label">Total turnover filed</div>
        </div>""", unsafe_allow_html=True)

with col3:
    if "net_assets" in df.columns:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-value">{fmt_currency(df["net_assets"].sum())}</div>
            <div class="metric-label">Total net assets</div>
        </div>""", unsafe_allow_html=True)

with col4:
    if employees_field:
        st.markdown(f"""
        <div class="metric-card">
            <div class="metric-value">{fmt_number(df[employees_field].sum())}</div>
            <div class="metric-label">Total employees</div>
        </div>""", unsafe_allow_html=True)

# ── Charts ────────────────────────────────────────────────────────────────────
st.markdown('<div class="section-header">Charts</div>', unsafe_allow_html=True)

chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    if employees_field and "period_start" in df.columns and df["period_start"].notna().any():
        emp_df = df[df[employees_field].notna()].copy()
        emp_df["month"] = emp_df["period_start"].dt.to_period("M").astype(str)
        monthly_emp = emp_df.groupby("month")[employees_field].sum().reset_index()
        monthly_emp.columns = ["Month", "Employees"]
        monthly_emp = monthly_emp.sort_values("Month")
        fig = px.bar(
            monthly_emp, x="Month", y="Employees",
            title="Total employees by period start month",
            color_discrete_sequence=["#1e3a5f"],
        )
        fig.update_layout(plot_bgcolor="white", paper_bgcolor="white",
                          margin=dict(l=0, r=0, t=40, b=0), title_font_size=14, xaxis_title=None)
        st.plotly_chart(fig, use_container_width=True)

with chart_col2:
    if "period_start" in df.columns and df["period_start"].notna().any():
        df["month"] = df["period_start"].dt.to_period("M").astype(str)
        monthly = df.groupby("month")[id_col].nunique().reset_index()
        monthly.columns = ["Month", "Companies"]
        monthly = monthly.sort_values("Month")
        fig2 = px.line(
            monthly, x="Month", y="Companies",
            title="Companies by period start month",
            color_discrete_sequence=["#1e3a5f"],
        )
        fig2.update_layout(plot_bgcolor="white", paper_bgcolor="white",
                           margin=dict(l=0, r=0, t=40, b=0), title_font_size=14, xaxis_title=None)
        fig2.update_traces(line_width=2)
        st.plotly_chart(fig2, use_container_width=True)

if turnover_field:
    turnover_df = df[df[turnover_field].notna() & (df[turnover_field] > 0)].copy()
    if not turnover_df.empty:
        chart_col3, chart_col4 = st.columns(2)

        with chart_col3:
            fig3 = px.histogram(turnover_df, x=turnover_field, nbins=50,
                                title="Turnover distribution (£)", log_x=True,
                                color_discrete_sequence=["#2e6da4"])
            fig3.update_layout(plot_bgcolor="white", paper_bgcolor="white",
                               margin=dict(l=0, r=0, t=40, b=0), title_font_size=14,
                               xaxis_title="Turnover (log scale)", yaxis_title="Count")
            st.plotly_chart(fig3, use_container_width=True)

        with chart_col4:
            def band(v):
                if v < 100_000: return "Under £100k"
                if v < 1_000_000: return "£100k–£1m"
                if v < 10_000_000: return "£1m–£10m"
                if v < 100_000_000: return "£10m–£100m"
                return "Over £100m"
            band_order = ["Under £100k", "£100k–£1m", "£1m–£10m", "£10m–£100m", "Over £100m"]
            turnover_df["band"] = turnover_df[turnover_field].apply(band)
            band_counts = (turnover_df.groupby("band")[id_col].nunique()
                           .reindex(band_order).fillna(0).reset_index())
            band_counts.columns = ["Turnover band", "Companies"]
            fig4 = px.bar(band_counts, x="Turnover band", y="Companies",
                          title="Companies by turnover band",
                          color_discrete_sequence=["#2e6da4"])
            fig4.update_layout(plot_bgcolor="white", paper_bgcolor="white",
                               margin=dict(l=0, r=0, t=40, b=0), title_font_size=14, xaxis_title=None)
            st.plotly_chart(fig4, use_container_width=True)

# ── Top companies table ───────────────────────────────────────────────────────
st.markdown('<div class="section-header">Top companies</div>', unsafe_allow_html=True)

rank_candidates = [
    ("turnover_gross_operating_revenue", "Turnover (gross operating revenue)"),
    ("turnover", "Turnover"),
    ("average_number_employees_during_period", "Average employees"),
    ("net_assets", "Net assets"),
    ("current_assets", "Current assets"),
    ("employees", "Employees"),
]
rank_options = [(col, label) for col, label in rank_candidates if col in df.columns]

if rank_options:
    rank_labels = [label for _, label in rank_options]
    rank_cols_map = {label: col for col, label in rank_options}
    chosen_label = st.selectbox("Rank by", rank_labels)
    rank_col = rank_cols_map[chosen_label]
    n_top = st.slider("Show top N", min_value=5, max_value=100, value=20, step=5)

    group_cols = [id_col] + ([name_col] if name_col else [])
    agg = {col: "max" for col, _ in rank_options}
    for extra in ["accounts_type", "company_dormant"]:
        if extra in df.columns:
            agg[extra] = "first"
    if "period_start" in df.columns:
        agg["period_start"] = "max"

    top_df = (
        df.groupby(group_cols).agg(agg)
        .reset_index()
        .sort_values(rank_col, ascending=False)
        .head(n_top)
        .copy()
    )

    # Format for display
    for c in ["turnover_gross_operating_revenue", "turnover", "net_assets", "current_assets"]:
        if c in top_df.columns:
            top_df[c] = top_df[c].apply(fmt_currency)
    for c in ["average_number_employees_during_period", "employees"]:
        if c in top_df.columns:
            top_df[c] = top_df[c].apply(lambda v: f"{int(v):,}" if pd.notna(v) else "—")

    top_df["companies_house_url"] = top_df[id_col].apply(
        lambda x: f"https://find-and-update.company-information.service.gov.uk/company/{x}"
    )

    st.dataframe(
        top_df, use_container_width=True, hide_index=True,
        column_config={
            "companies_house_url": st.column_config.LinkColumn("Companies House", display_text="View ↗"),
            "period_start": st.column_config.DateColumn("Period start", format="DD/MM/YYYY"),
        }
    )

# ── Search all companies ──────────────────────────────────────────────────────
st.markdown('<div class="section-header">Search all companies</div>', unsafe_allow_html=True)

search = st.text_input("Search by company name or number", placeholder="e.g. Tesco or 00000001")

search_df = df.copy()
if search.strip():
    mask = pd.Series(False, index=search_df.index)
    if name_col and name_col in search_df.columns:
        mask |= search_df[name_col].astype(str).str.contains(search, case=False, na=False)
    mask |= search_df[id_col].astype(str).str.contains(search, case=False, na=False)
    search_df = search_df[mask]
    st.caption(f"{len(search_df):,} rows match '{search}'")

search_df["companies_house_url"] = search_df[id_col].apply(
    lambda x: f"https://find-and-update.company-information.service.gov.uk/company/{x}"
)

priority_cols = [c for c in [
    id_col, name_col,
    "period_start", "period_end",
    "accounts_type", "company_dormant",
    "turnover_gross_operating_revenue", "turnover",
    "net_assets", "current_assets",
    "average_number_employees_during_period", "employees",
    "companies_house_url",
] if c and c in search_df.columns]

st.dataframe(
    search_df[priority_cols].head(500),
    use_container_width=True,
    hide_index=True,
    column_config={
        "companies_house_url": st.column_config.LinkColumn("Companies House", display_text="View ↗"),
        "period_start": st.column_config.DateColumn("Period start", format="DD/MM/YYYY"),
        "period_end": st.column_config.DateColumn("Period end", format="DD/MM/YYYY"),
    }
)

if len(search_df) > 500:
    st.caption("Showing first 500 rows. Use filters or search to narrow down.")

# ── UKIXBRL Viewer lookup ─────────────────────────────────────────────────────
st.markdown('<div class="section-header">🔍 View filing on UK iXBRL Viewer</div>', unsafe_allow_html=True)
st.markdown(
    "Enter a company number to open its iXBRL filings directly on the "
    "[UK iXBRL Viewer](https://ukixbrlviewer.org.uk)."
)

viewer_col1, viewer_col2 = st.columns([2, 3])

with viewer_col1:
    viewer_company = st.text_input(
        "Company number",
        placeholder="e.g. 00012345",
        key="viewer_lookup",
        label_visibility="collapsed",
    )

with viewer_col2:
    if viewer_company.strip():
        cn = viewer_company.strip().zfill(8)  # pad to 8 digits like CoHo format
        viewer_url = f"https://ukixbrlviewer.org.uk/?search={cn}"
        st.markdown(
            f"""
            <a href="{viewer_url}" target="_blank" style="
                display: inline-block;
                background-color: #1e3a5f;
                color: white;
                padding: 0.45rem 1.1rem;
                border-radius: 6px;
                text-decoration: none;
                font-size: 0.9rem;
                font-weight: 500;
                margin-top: 0.1rem;
            ">Open on UK iXBRL Viewer ↗</a>
            <span style="font-size:0.8rem; color:#64748b; margin-left:0.75rem;">
                Company number: <strong>{cn}</strong>
            </span>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<span style='color:#94a3b8; font-size:0.9rem;'>Enter a company number to generate a link</span>",
            unsafe_allow_html=True,
        )

st.caption(
    "The UK iXBRL Viewer lets you browse the full tagged iXBRL report for any "
    "Companies House filing. If the company number doesn't return results, try "
    "searching by company name on the viewer directly."
)

# ── Download ──────────────────────────────────────────────────────────────────
st.markdown('<div class="section-header">Download</div>', unsafe_allow_html=True)

buf = io.StringIO()
df.to_csv(buf, index=False)
st.download_button(
    label=f"⬇️ Download filtered data ({len(df):,} rows)",
    data=buf.getvalue().encode("utf-8"),
    file_name="xbrl_filtered.csv",
    mime="text/csv",
)
