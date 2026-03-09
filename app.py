import multiprocessing
multiprocessing.set_start_method('fork', force=True)  # needed for stream-read-xbrl on some platforms

import io
import re
import zipfile

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from stream_read_xbrl import stream_read_xbrl_zip

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Companies House XBRL → CSV",
    page_icon="🏢",
    layout="wide",
)

# ── Load CC lookup (committed to repo as cc_lookup.parquet) ───────────────────
@st.cache_data
def load_cc_lookup():
    try:
        df = pd.read_parquet("cc_lookup.parquet")
        return df
    except FileNotFoundError:
        return None

cc_lookup = load_cc_lookup()

# ── uk-bus enrichment helpers ─────────────────────────────────────────────────
UK_BUS_URIS = {
    'http://xbrl.frc.org.uk/cd/2021-01-01/business',
    'http://xbrl.frc.org.uk/cd/2022-01-01/business',
    'http://xbrl.frc.org.uk/cd/2023-01-01/business',
    'http://xbrl.frc.org.uk/fr/2014-09-01/business',
    'http://xbrl.frc.org.uk/fr/2016-01-01/business',
}
TARGET_FIELDS = [
    'PrincipalLocation-CityOrTown',
    'NameEntityAuditors',
    'AccountsStatusAuditedOrUnaudited',
    'AccountsType',
    'DescriptionPrincipalActivities',
]

def clean_uri_value(value):
    if value and '#' in value:
        return value.split('#')[-1]
    return value

def extract_uk_bus_fields(content: bytes) -> dict:
    from lxml import etree
    result = {f: None for f in TARGET_FIELDS}
    try:
        tree = etree.fromstring(content)
    except etree.XMLSyntaxError:
        try:
            from lxml import html as lxml_html
            tree = lxml_html.fromstring(content)
        except Exception:
            return result
    for elem in tree.iter():
        tag = elem.get('name', '')
        if not tag or ':' not in tag:
            continue
        prefix, local_name = tag.split(':', 1)
        if local_name not in TARGET_FIELDS:
            continue
        ns_uri = elem.nsmap.get(prefix, '')
        if ns_uri not in UK_BUS_URIS:
            continue
        text = ''.join(elem.itertext()).strip()
        if text and result[local_name] is None:
            result[local_name] = text
    result['AccountsType'] = clean_uri_value(result['AccountsType'])
    result['AccountsStatusAuditedOrUnaudited'] = clean_uri_value(result['AccountsStatusAuditedOrUnaudited'])
    return result

def get_company_number_from_name(name: str):
    parts = name.replace('\\', '/').split('/')
    fname = parts[-1]
    for seg in fname.split('_'):
        clean = seg.split('.')[0]
        if re.match(r'^[A-Z]{0,2}\d{6,8}$', clean):
            return clean.zfill(8)
    return None

def run_enrichment(file_bytes: bytes) -> pd.DataFrame:
    records = []
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(('.html', '.xhtml', '.xml'))]
        progress = st.progress(0, text="Enriching records…")
        for i, name in enumerate(names):
            progress.progress((i + 1) / len(names), text=f"Enriching {i+1:,} / {len(names):,} documents…")
            company_number = get_company_number_from_name(name)
            if not company_number:
                continue
            try:
                with zf.open(name) as f:
                    content = f.read()
                fields = extract_uk_bus_fields(content)
                fields['_company_number'] = company_number
                records.append(fields)
            except Exception:
                continue
        progress.empty()
    extra = pd.DataFrame(records).rename(columns={
        'PrincipalLocation-CityOrTown': 'city_or_town',
        'NameEntityAuditors': 'auditor_name',
        'AccountsStatusAuditedOrUnaudited': 'audit_status',
        'AccountsType': 'accounts_type',
        'DescriptionPrincipalActivities': 'principal_activities',
    })
    return extra

# ── Charity join ──────────────────────────────────────────────────────────────
def join_charities(df: pd.DataFrame, cc: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df['_coho_num'] = df['company_id'].astype(str).str.strip().str.zfill(8)
    merged = df.merge(cc, left_on='_coho_num', right_on='coho_number', how='left')
    merged.drop(columns=['_coho_num', 'coho_number'], inplace=True, errors='ignore')
    merged['is_charity'] = merged['registered_charity_number'].notna()
    merged['is_registered_charity'] = merged['charity_registration_status'] == 'Registered'
    return merged

# ── Dashboard ─────────────────────────────────────────────────────────────────
def show_dashboard(df: pd.DataFrame, date_label: str):
    charity_df = df[df['is_registered_charity']].copy()
    total = len(df)
    n_charities = len(charity_df)

    C_BLUE  = '#2563EB'
    C_GREEN = '#16A34A'
    C_GREY  = '#E5E7EB'
    FONT    = 'Inter, Arial, sans-serif'
    BG      = '#F9FAFB'

    def base(title=''):
        return dict(
            title=dict(text=title, font=dict(family=FONT, size=14, color='#111827')),
            paper_bgcolor=BG, plot_bgcolor=BG,
            font=dict(family=FONT, size=12, color='#374151'),
            margin=dict(l=40, r=20, t=50, b=40),
        )

    # KPIs
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total filings", f"{total:,}")
    k2.metric("Charity filings", f"{n_charities:,}", f"{n_charities/total*100:.1f}% of total" if total else None)
    cio_count = int((charity_df['charity_is_cio'] == True).sum()) if 'charity_is_cio' in charity_df.columns else 0
    k3.metric("CIOs", f"{cio_count:,}")
    audit_count = int((charity_df['audit_status'] == 'audited').sum()) if 'audit_status' in charity_df.columns else 0
    k4.metric("Audited charities", f"{audit_count:,}" if 'audit_status' in charity_df.columns else "—")
    insolvent = int(charity_df['charity_insolvent'].sum()) if 'charity_insolvent' in charity_df.columns else 0
    k5.metric("Insolvent", f"{insolvent:,}" if 'charity_insolvent' in charity_df.columns else "—")

    st.divider()

    # Row 1: split pie + accounts type
    col1, col2 = st.columns(2)
    with col1:
        fig = go.Figure(go.Pie(
            labels=['Registered charity', 'Other'],
            values=[n_charities, total - n_charities],
            marker_colors=[C_BLUE, C_GREY],
            hole=0.45, textinfo='label+percent',
            hovertemplate='%{label}: %{value:,}<extra></extra>'
        ))
        fig.update_layout(**base('Charity vs non-charity filings'), height=320, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with col2:
        if 'accounts_type' in charity_df.columns:
            at = charity_df['accounts_type'].fillna('not tagged').value_counts().head(8)
            fig = go.Figure(go.Bar(
                x=at.values, y=at.index, orientation='h',
                marker_color=C_BLUE,
                hovertemplate='%{y}: %{x:,}<extra></extra>'
            ))
            fig.update_layout(**base('Accounts type (charities)'), height=320, showlegend=False,
                              xaxis=dict(showgrid=True, gridcolor=C_GREY),
                              yaxis=dict(showgrid=False))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Enable 'Additional enrichment' to see accounts type breakdown.")

    # Row 2: cities + audit status
    col3, col4 = st.columns(2)
    with col3:
        if 'city_or_town' in charity_df.columns:
            cities = (charity_df['city_or_town'].str.title().str.strip()
                      .replace('', pd.NA).dropna()
                      .value_counts().head(15))
            fig = go.Figure(go.Bar(
                x=cities.values, y=cities.index, orientation='h',
                marker_color=C_BLUE,
                hovertemplate='%{y}: %{x:,}<extra></extra>'
            ))
            fig.update_layout(**base('Top 15 cities (charity filings)'), height=400,
                              xaxis=dict(showgrid=True, gridcolor=C_GREY),
                              yaxis=dict(showgrid=False, tickfont=dict(size=11)))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Enable 'Additional enrichment' to see city breakdown.")

    with col4:
        if 'audit_status' in charity_df.columns:
            audit = charity_df['audit_status'].fillna('not tagged').value_counts()
            fig = go.Figure(go.Pie(
                labels=audit.index, values=audit.values,
                marker_colors=[C_GREEN, C_GREY, '#93C5FD'],
                hole=0.45, textinfo='label+percent',
                hovertemplate='%{label}: %{value:,}<extra></extra>'
            ))
            fig.update_layout(**base('Audit status (charities)'), height=400, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Enable 'Additional enrichment' to see audit status breakdown.")

    # Row 3: income distribution
    if 'charity_latest_income' in charity_df.columns:
        inc = pd.to_numeric(charity_df['charity_latest_income'], errors='coerce').dropna()
        inc = inc[inc > 0]
        if len(inc) > 0:
            bands = pd.cut(inc,
                bins=[0, 10_000, 100_000, 500_000, 1_000_000, 5_000_000, float('inf')],
                labels=['Under £10k', '£10k–£100k', '£100k–£500k', '£500k–£1m', '£1m–£5m', 'Over £5m']
            ).value_counts().sort_index()
            fig = go.Figure(go.Bar(
                x=bands.index.astype(str), y=bands.values,
                marker_color=C_BLUE,
                hovertemplate='%{x}: %{y:,} charities<extra></extra>'
            ))
            fig.update_layout(**base('Latest income distribution (from Charity Commission)'),
                              height=320,
                              xaxis=dict(showgrid=False),
                              yaxis=dict(showgrid=True, gridcolor=C_GREY))
            st.plotly_chart(fig, use_container_width=True)

    # Row 4: top auditors
    if 'auditor_name' in charity_df.columns:
        auditors = (charity_df['auditor_name'].str.strip().str.title()
                    .replace('', pd.NA).dropna()
                    .value_counts().head(15))
        if len(auditors) > 0:
            fig = go.Figure(go.Bar(
                x=auditors.values, y=auditors.index, orientation='h',
                marker_color=C_GREEN,
                hovertemplate='%{y}: %{x:,}<extra></extra>'
            ))
            fig.update_layout(**base('Top 15 auditors (charity filings)'), height=420,
                              xaxis=dict(showgrid=True, gridcolor=C_GREY),
                              yaxis=dict(showgrid=False, tickfont=dict(size=11)))
            st.plotly_chart(fig, use_container_width=True)

    # Charity table
    st.subheader("Charity filings")
    show_cols = [c for c in [
        'company_id', 'entity_current_legal_name', 'registered_charity_number',
        'charity_name', 'charity_registration_status', 'accounts_type',
        'audit_status', 'auditor_name', 'city_or_town', 'charity_latest_income',
    ] if c in df.columns]
    st.dataframe(charity_df[show_cols], use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════════════════════

st.title("🏢 Companies House XBRL → CSV")
st.markdown(
    "Upload a bulk accounts ZIP from "
    "[Companies House](http://download.companieshouse.gov.uk/en_accountsdata.html) "
    "to parse it into a CSV — with optional charity detection and dashboard."
)

if cc_lookup is None:
    st.warning("⚠️ `cc_lookup.parquet` not found in the repo — charity detection will be unavailable.")

st.info(
    "**How to get the file:** Go to the Companies House bulk data page, download any "
    "`Accounts_Bulk_Data-YYYY-MM-DD.zip` file, then upload it here.",
    icon="ℹ️",
)

# ── Options ───────────────────────────────────────────────────────────────────
with st.expander("⚙️ Options", expanded=True):
    col_a, col_b = st.columns(2)
    with col_a:
        do_charity = st.checkbox(
            "🔍 Charity detection",
            value=True,
            disabled=(cc_lookup is None),
            help="Cross-reference against the Charity Commission register. Adds is_charity, charity_name, income etc."
        )
    with col_b:
        do_enrichment = st.checkbox(
            "🔬 Additional enrichment (slower — 5–10 min)",
            value=False,
            help="Extracts auditor name, city/town, accounts type, audit status and principal activities from the raw iXBRL. Adds 5 extra columns but takes significantly longer."
        )

# ── File uploader ─────────────────────────────────────────────────────────────
uploaded = st.file_uploader(
    "Upload Companies House bulk accounts ZIP",
    type=["zip"],
    help="Files are typically 50–300 MB.",
)

if uploaded is not None:
    filename = uploaded.name
    csv_name = filename.replace(".zip", ".csv").replace(".ZIP", ".csv")
    date_label = re.search(r'\d{4}-\d{2}-\d{2}', filename)
    date_label = date_label.group() if date_label else filename

    st.write(f"**File:** `{filename}`  |  **Size:** {uploaded.size / 1_048_576:.1f} MB")

    file_bytes = uploaded.read()

    # ── Step 1: Parse XBRL ───────────────────────────────────────────────────
    with st.spinner("Parsing XBRL data — usually 10–30 seconds…"):
        try:
            def byte_chunks(data, chunk_size=65_536):
                for i in range(0, len(data), chunk_size):
                    yield data[i: i + chunk_size]

            with stream_read_xbrl_zip(byte_chunks(file_bytes)) as (columns, rows):
                df = pd.DataFrame(rows, columns=columns)
        except Exception as e:
            st.error(f"**Error during parsing:** {e}")
            st.stop()

    st.success(f"✅ Parsed **{len(df):,} rows** and **{len(df.columns)} columns**.")

    # ── Step 2: uk-bus enrichment (optional) ─────────────────────────────────
    if do_enrichment:
        with st.spinner("Running additional enrichment — this takes 5–10 minutes…"):
            try:
                extra = run_enrichment(file_bytes)
                if len(extra) > 0:
                    df['_company_number'] = df['company_id'].astype(str).str.strip().str.zfill(8)
                    df = df.merge(extra, on='_company_number', how='left')
                    df.drop(columns=['_company_number'], inplace=True, errors='ignore')
                    st.success(f"✅ Enrichment complete — added 5 extra columns.")
            except Exception as e:
                st.warning(f"Enrichment failed: {e} — continuing without extra fields.")

    # ── Step 3: Charity lookup (optional) ────────────────────────────────────
    if do_charity and cc_lookup is not None:
        df = join_charities(df, cc_lookup)
        n_charities = int(df['is_registered_charity'].sum())
        st.success(f"✅ Matched **{n_charities:,}** registered charities in this filing.")

    # ── Tabs ──────────────────────────────────────────────────────────────────
    if do_charity and cc_lookup is not None and 'is_charity' in df.columns:
        tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "📋 Data", "⬇️ Download"])
    else:
        tab1, tab2, tab3 = None, None, None
        tab2, tab3 = st.tabs(["📋 Data", "⬇️ Download"])

    if tab1:
        with tab1:
            show_dashboard(df, date_label)

    data_tab = tab2 if tab1 else tab2
    with data_tab:
        st.subheader("Preview (first 100 rows)")
        st.dataframe(df.head(100), use_container_width=True)

        with st.expander("📋 Column summary"):
            summary = pd.DataFrame({
                "column": df.columns,
                "dtype": df.dtypes.values,
                "non_null": df.notna().sum().values,
                "null_%": (df.isna().mean() * 100).round(1).values,
                "sample": [df[c].dropna().iloc[0] if df[c].notna().any() else "" for c in df.columns],
            })
            st.dataframe(summary, use_container_width=True, hide_index=True)

        with st.expander("🔍 Filter before downloading (optional)"):
            col1, col2 = st.columns(2)
            with col1:
                company_filter = st.text_input(
                    "Filter by company number(s)",
                    placeholder="e.g. 00012345, 00067890 (comma-separated)",
                ) if "company_id" in df.columns else ""
            with col2:
                numeric_cols = df.select_dtypes(include="number").columns.tolist()
                keep_non_null = st.multiselect(
                    "Only keep rows where these columns are non-null",
                    options=numeric_cols, default=[],
                ) if numeric_cols else []

            filtered_df = df.copy()
            if company_filter.strip():
                ids = [c.strip() for c in company_filter.split(",") if c.strip()]
                filtered_df = filtered_df[filtered_df["company_id"].astype(str).isin(ids)]
                st.write(f"Filtered to **{len(filtered_df):,} rows**.")
            for col in keep_non_null:
                filtered_df = filtered_df[filtered_df[col].notna()]
            if keep_non_null:
                st.write(f"After non-null filter: **{len(filtered_df):,} rows**.")

    export_df = filtered_df if (company_filter.strip() or keep_non_null) else df

    with tab3:
        csv_buffer = io.StringIO()
        export_df.to_csv(csv_buffer, index=False)
        csv_bytes = csv_buffer.getvalue().encode("utf-8")

        st.download_button(
            label=f"⬇️ Download CSV ({len(export_df):,} rows)",
            data=csv_bytes,
            file_name=csv_name,
            mime="text/csv",
        )
        st.caption(
            f"Exporting {len(export_df):,} rows × {len(export_df.columns)} columns  |  "
            f"~{len(csv_bytes) / 1_048_576:.1f} MB"
        )

else:
    st.markdown("---")
    st.markdown("### What you get")
    st.markdown(
        "The output is a structured dataframe with one row per tagged financial fact, including:\n\n"
        "- `company_id`, `company_name`, `balance_sheet_date`\n"
        "- Financial figures: `turnover`, `net_assets`, `current_assets`, `cash`, `employees`\n"
        "- Filing metadata: `accounts_type`, `period_start`, `period_end`\n"
        "- **Charity flag** (optional): `is_charity`, `registered_charity_number`, `charity_name`, `charity_latest_income` and more\n"
        "- **Extra enrichment** (optional): `auditor_name`, `city_or_town`, `audit_status`, `principal_activities`\n\n"
        "Parsing uses the UK government's open-source "
        "[stream-read-xbrl](https://github.com/uktrade/stream-read-xbrl) library."
    )
