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
# Match any FRC "business" namespace regardless of taxonomy year (cd/fr, 2014→future).
# Previously a hardcoded set that stopped at 2023, which silently dropped ~90% of
# 2024/2025 filings' entity info. Pattern-based match is forward-compatible.
UK_BUS_URI_RE = re.compile(r'xbrl\.frc\.org\.uk/.+/business$')
TARGET_FIELDS = [
    'PrincipalLocation-CityOrTown',
    'NameEntityAuditors',
    'AccountsStatusAuditedOrUnaudited',
    'AccountsType',
    'DescriptionPrincipalActivities',
    'PostalCodeZip',
]

def clean_uri_value(value):
    if value and '#' in value:
        return value.split('#')[-1]
    return value

def normalise_postcode(value):
    """Tidy a raw tagged postcode to canonical 'AB12 3CD' form so it's
    join-ready for downstream geography lookups. Conservative: if it doesn't
    look like a UK postcode, just return it stripped/uppercased rather than
    discarding it, so coverage figures stay honest."""
    if not value:
        return None
    pc = re.sub(r'\s+', '', str(value)).upper()
    # UK postcodes are 5–7 chars; the inward code is always the final 3 (digit+2 letters)
    if 5 <= len(pc) <= 7 and pc[-3].isdigit():
        return f"{pc[:-3]} {pc[-3:]}"
    return str(value).strip().upper() or None

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
        if not isinstance(elem.tag, str):  # skip comments / processing instructions
            continue
        tag = elem.get('name') or ''
        if not tag or ':' not in tag:
            continue
        prefix, local_name = tag.split(':', 1)
        # Postcode can appear under slightly different local names across
        # taxonomy versions / address blocks, so match it fuzzily; everything
        # else must be an exact target.
        if local_name in TARGET_FIELDS:
            field_key = local_name
        elif 'PostalCode' in local_name:
            field_key = 'PostalCodeZip'
        else:
            continue
        ns_uri = (elem.nsmap or {}).get(prefix, '') or ''
        if not UK_BUS_URI_RE.search(ns_uri):
            continue
        text = ''.join(elem.itertext()).strip()
        if text and result[field_key] is None:
            result[field_key] = text
    result['AccountsType'] = clean_uri_value(result['AccountsType'])
    result['AccountsStatusAuditedOrUnaudited'] = clean_uri_value(result['AccountsStatusAuditedOrUnaudited'])
    result['PostalCodeZip'] = normalise_postcode(result['PostalCodeZip'])
    return result

def extract_entity_name(content: bytes):
    """Grab the tagged legal/registered name (uk-bus:EntityCurrentLegalOrRegisteredName).
    Used for CIC rows, which the core parser skips, so they'd otherwise be nameless."""
    from lxml import etree
    try:
        tree = etree.fromstring(content)
    except etree.XMLSyntaxError:
        try:
            from lxml import html as lxml_html
            tree = lxml_html.fromstring(content)
        except Exception:
            return None
    for elem in tree.iter():
        if not isinstance(elem.tag, str):
            continue
        tag = elem.get('name') or ''
        if tag.endswith(':EntityCurrentLegalOrRegisteredName'):
            ns = (elem.nsmap or {}).get(tag.split(':', 1)[0], '') or ''
            if UK_BUS_URI_RE.search(ns):
                txt = ''.join(elem.itertext()).strip()
                if txt:
                    return txt
    return None

def extract_from_zip_member(zip_bytes: bytes) -> dict:
    """Extract uk-bus fields from a filing that is itself a zip nested inside the
    bulk zip (e.g. Community Interest Company `*_CIC.zip` packages). Merges across
    the inner documents — the accounts file first, as it carries the address block —
    taking the first non-null value per field."""
    merged = {f: None for f in TARGET_FIELDS}
    entity_name = None
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as inner:
            inner_names = [n for n in inner.namelist()
                           if n.lower().endswith(('.html', '.xhtml', '.xml'))]
            # parse the accounts document before the CIC34 report (richer entity info)
            inner_names.sort(key=lambda n: 0 if 'account' in n.lower() else 1)
            for inm in inner_names:
                try:
                    content = inner.read(inm)
                except Exception:
                    continue
                fields = extract_uk_bus_fields(content)
                for k, v in fields.items():
                    if merged[k] is None and v:
                        merged[k] = v
                if entity_name is None:
                    entity_name = extract_entity_name(content)
    except Exception:
        pass
    merged['_entity_name'] = entity_name
    return merged

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
        doc_names = [n for n in zf.namelist() if n.lower().endswith(('.html', '.xhtml', '.xml'))]
        # CIC filings arrive as a zip nested inside the bulk zip (e.g. *_CIC.zip),
        # which the core parser skips — recurse into them so they're not lost.
        zip_names = [n for n in zf.namelist() if n.lower().endswith('.zip')]
        all_names = doc_names + zip_names
        total = len(all_names)
        progress = st.progress(0, text="Enriching records…")
        for i, name in enumerate(all_names):
            progress.progress((i + 1) / total, text=f"Enriching {i+1:,} / {total:,} documents…")
            company_number = get_company_number_from_name(name)
            if not company_number:
                continue
            try:
                if name.lower().endswith('.zip'):
                    fields = extract_from_zip_member(zf.read(name))
                    fields['_is_cic'] = True
                else:
                    with zf.open(name) as f:
                        content = f.read()
                    fields = extract_uk_bus_fields(content)
                    fields['_entity_name'] = None
                    fields['_is_cic'] = False
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
        'PostalCodeZip': 'postcode',
        '_is_cic': 'is_cic',
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

# ── Geographic lookup ─────────────────────────────────────────────────────────
# Approximate centroids for every UK postcode area (the 1–2 leading letters).
# Bundled so the map works offline with no external geocoding call. Accuracy is
# ~town level, which is all a national bubble map needs. For street-level points
# you'd swap this for a postcodes.io lookup (see notes in the app footer).
POSTCODE_AREA_CENTROIDS = {
    'AB': (57.15, -2.10), 'AL': (51.75, -0.33), 'B': (52.48, -1.90), 'BA': (51.38, -2.36),
    'BB': (53.75, -2.48), 'BD': (53.79, -1.75), 'BH': (50.72, -1.88), 'BL': (53.58, -2.43),
    'BN': (50.83, -0.14), 'BR': (51.40, 0.01), 'BS': (51.45, -2.59), 'BT': (54.60, -5.93),
    'CA': (54.89, -2.93), 'CB': (52.20, 0.12), 'CF': (51.48, -3.18), 'CH': (53.19, -2.89),
    'CM': (51.73, 0.47), 'CO': (51.89, 0.90), 'CR': (51.37, -0.10), 'CT': (51.28, 1.08),
    'CV': (52.41, -1.51), 'CW': (53.10, -2.44), 'DA': (51.44, 0.22), 'DD': (56.46, -2.97),
    'DE': (52.92, -1.48), 'DG': (55.07, -3.60), 'DH': (54.78, -1.58), 'DL': (54.53, -1.55),
    'DN': (53.52, -1.13), 'DT': (50.71, -2.44), 'DY': (52.51, -2.09), 'E': (51.53, 0.03),
    'EC': (51.52, -0.09), 'EH': (55.95, -3.19), 'EN': (51.65, -0.08), 'EX': (50.72, -3.53),
    'FK': (56.00, -3.78), 'FY': (53.82, -3.05), 'G': (55.86, -4.25), 'GL': (51.86, -2.24),
    'GU': (51.24, -0.57), 'GY': (49.45, -2.58), 'HA': (51.58, -0.34), 'HD': (53.65, -1.78),
    'HG': (54.00, -1.54), 'HP': (51.75, -0.47), 'HR': (52.06, -2.72), 'HS': (57.90, -6.80),
    'HU': (53.74, -0.33), 'HX': (53.72, -1.86), 'IG': (51.56, 0.07), 'IM': (54.15, -4.48),
    'IP': (52.06, 1.15), 'IV': (57.48, -4.22), 'JE': (49.21, -2.13), 'KA': (55.61, -4.50),
    'KT': (51.41, -0.30), 'KW': (58.98, -2.96), 'KY': (56.11, -3.16), 'L': (53.41, -2.98),
    'LA': (54.05, -2.80), 'LD': (52.24, -3.38), 'LE': (52.64, -1.13), 'LL': (53.32, -3.83),
    'LN': (53.23, -0.54), 'LS': (53.80, -1.55), 'LU': (51.88, -0.42), 'M': (53.48, -2.24),
    'ME': (51.38, 0.53), 'MK': (52.04, -0.76), 'ML': (55.79, -3.99), 'N': (51.56, -0.11),
    'NE': (54.98, -1.61), 'NG': (52.95, -1.15), 'NN': (52.24, -0.90), 'NP': (51.59, -3.00),
    'NR': (52.63, 1.30), 'NW': (51.55, -0.20), 'OL': (53.54, -2.11), 'OX': (51.75, -1.26),
    'PA': (55.85, -4.43), 'PE': (52.57, -0.24), 'PH': (56.40, -3.44), 'PL': (50.38, -4.14),
    'PO': (50.80, -1.09), 'PR': (53.76, -2.70), 'RG': (51.46, -0.97), 'RH': (51.24, -0.17),
    'RM': (51.58, 0.18), 'S': (53.38, -1.47), 'SA': (51.62, -3.94), 'SE': (51.47, -0.05),
    'SG': (51.90, -0.20), 'SK': (53.41, -2.16), 'SL': (51.51, -0.59), 'SM': (51.36, -0.19),
    'SN': (51.56, -1.78), 'SO': (50.90, -1.40), 'SP': (51.07, -1.79), 'SR': (54.91, -1.38),
    'SS': (51.54, 0.71), 'ST': (53.00, -2.18), 'SW': (51.46, -0.16), 'SY': (52.71, -2.75),
    'TA': (51.02, -3.10), 'TD': (55.61, -2.80), 'TF': (52.68, -2.45), 'TN': (51.13, 0.27),
    'TQ': (50.46, -3.53), 'TR': (50.26, -5.05), 'TS': (54.57, -1.23), 'TW': (51.44, -0.33),
    'UB': (51.55, -0.44), 'W': (51.51, -0.20), 'WA': (53.39, -2.60), 'WC': (51.52, -0.12),
    'WD': (51.66, -0.40), 'WF': (53.68, -1.50), 'WN': (53.55, -2.63), 'WR': (52.19, -2.22),
    'WS': (52.59, -1.98), 'WV': (52.59, -2.13), 'YO': (53.96, -1.08), 'ZE': (60.15, -1.15),
}

def postcode_area(series: pd.Series) -> pd.Series:
    """Outward-code area letters (e.g. 'SE13 7SL' -> 'SE')."""
    return series.astype(str).str.strip().str.upper().str.extract(r'^([A-Z]{1,2})')[0]


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

    NA_COL  = 'net_assets_liabilities_including_pension_asset_liability'
    EMP_COL = 'average_number_employees_during_period'

    # ══ All filings — population overview ═════════════════════════════════════
    st.subheader(f"All filings — {date_label}")
    net_assets_all = pd.to_numeric(df[NA_COL], errors='coerce') if NA_COL in df.columns else pd.Series(dtype=float)
    dormant_n = int((df['company_dormant'] == True).sum()) if 'company_dormant' in df.columns else 0
    cic_n = int(df['is_cic'].sum()) if 'is_cic' in df.columns else 0
    pc_all = df['postcode'].dropna().astype(str) if 'postcode' in df.columns else pd.Series(dtype=str)
    pc_cov = len(pc_all) / total * 100 if total else 0

    o1, o2, o3, o4, o5 = st.columns(5)
    o1.metric("Total filings", f"{total:,}")
    o2.metric("Dormant", f"{dormant_n:,}", f"{dormant_n/total*100:.0f}% of filings" if total else None)
    o3.metric("With postcode", f"{pc_cov:.0f}%", f"{len(pc_all):,} tagged")
    o4.metric("Median net assets",
              f"£{net_assets_all.dropna().median():,.0f}" if net_assets_all.notna().any() else "—")
    o5.metric("CIC recovered", f"{cic_n:,}")

    # ══ Geographic map + regional analysis ═══════════════════════════════════
    if len(pc_all):
        g = df.copy()
        g['_area'] = postcode_area(g['postcode'])
        g = g[g['_area'].notna()]
        g['_na'] = pd.to_numeric(g[NA_COL], errors='coerce') if NA_COL in g.columns else pd.NA
        g['_emp'] = pd.to_numeric(g[EMP_COL], errors='coerce') if EMP_COL in g.columns else pd.NA
        g['_dormant'] = (g['company_dormant'] == True) if 'company_dormant' in g.columns else False

        agg = g.groupby('_area').agg(
            filings=('_area', 'size'),
            median_net_assets=('_na', 'median'),
            median_employees=('_emp', 'median'),
            pct_dormant=('_dormant', 'mean'),
        ).reset_index()
        agg['pct_dormant'] = agg['pct_dormant'] * 100
        agg['lat'] = agg['_area'].map(lambda a: POSTCODE_AREA_CENTROIDS.get(a, (None, None))[0])
        agg['lon'] = agg['_area'].map(lambda a: POSTCODE_AREA_CENTROIDS.get(a, (None, None))[1])
        mapped = agg[agg['lat'].notna()].copy()
        missing = sorted(agg[agg['lat'].isna()]['_area'].dropna().tolist())

        METRICS = {
            'Filing count':        ('filings', ':,.0f'),
            'Median net assets':   ('median_net_assets', ':,.0f'),
            'Median employees':    ('median_employees', ':,.1f'),
            '% dormant':           ('pct_dormant', ':.0f'),
        }
        choice = st.selectbox("Metric", list(METRICS.keys()), index=0,
                              help="Drives the map colour and the ranking on the right.")
        mcol, mfmt = METRICS[choice]

        map_col, bar_col = st.columns([3, 2])
        with map_col:
            vals = pd.to_numeric(mapped[mcol], errors='coerce')
            sizes = mapped['filings'].astype(float)
            hover = [
                f"<b>{a}</b><br>{int(f):,} filings"
                f"<br>median net assets: £{na:,.0f}" if pd.notna(na) else f"<b>{a}</b><br>{int(f):,} filings"
                for a, f, na in zip(mapped['_area'], mapped['filings'], mapped['median_net_assets'])
            ]
            fig = go.Figure(go.Scattergeo(
                lon=mapped['lon'], lat=mapped['lat'], text=hover,
                marker=dict(
                    size=sizes, sizemode='area',
                    sizeref=2.0 * sizes.max() / (38.0 ** 2), sizemin=3,
                    color=vals, colorscale='Blues', showscale=True,
                    colorbar=dict(title=dict(text=choice, side='right'), thickness=12, len=0.8),
                    line=dict(width=0.5, color='white'), opacity=0.9,
                ),
                hovertemplate='%{text}<extra></extra>',
            ))
            fig.update_geos(
                scope='europe', resolution=50,
                lataxis_range=[49.8, 61.0], lonaxis_range=[-8.5, 2.0],
                showcountries=True, countrycolor='#D1D5DB',
                showland=True, landcolor='#FFFFFF',
                showocean=True, oceancolor=BG, showlakes=False, showframe=False,
            )
            map_layout = base(f'Filings by postcode area — {choice.lower()}')
            map_layout['margin'] = dict(l=0, r=0, t=50, b=0)
            map_layout['height'] = 540
            fig.update_layout(**map_layout)
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                f"Registered-office postcode ({pc_cov:.0f}% of filings tagged) — shows where companies "
                "are *registered*, not where they operate, so expect a London/agent-address skew."
                + (f" {len(missing)} area(s) without a centroid not shown: {', '.join(missing[:10])}." if missing else "")
            )
        with bar_col:
            top = mapped.sort_values(mcol, ascending=False).head(15)
            fig2 = go.Figure(go.Bar(
                x=pd.to_numeric(top[mcol], errors='coerce'), y=top['_area'],
                orientation='h', marker_color=C_BLUE,
                hovertemplate='%{y}: %{x' + mfmt + '}<extra></extra>',
            ))
            fig2.update_layout(**base(f'Top 15 areas — {choice.lower()}'), height=540,
                               xaxis=dict(showgrid=True, gridcolor=C_GREY),
                               yaxis=dict(showgrid=False, autorange='reversed', tickfont=dict(size=11)))
            st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("Enable **Additional enrichment** when parsing to unlock the geographic map "
                "(it needs the tagged postcode field).")

    # ══ Tagging coverage (data-quality signal) ═══════════════════════════════
    st.markdown("**Tagging coverage** — how completely this batch populated key fields")
    qmap = [
        ('Legal name', 'entity_current_legal_name'), ('Balance-sheet date', 'balance_sheet_date'),
        ('Dormant flag', 'company_dormant'), ('Employees', EMP_COL),
        ('Net assets', NA_COL), ('Postcode', 'postcode'),
        ('City / town', 'city_or_town'), ('Auditor', 'auditor_name'),
    ]
    qrows = [(lbl, df[c].notna().mean() * 100) for lbl, c in qmap if c in df.columns]
    if qrows:
        qrows.sort(key=lambda r: r[1])
        labels = [r[0] for r in qrows]
        covs = [r[1] for r in qrows]
        figq = go.Figure(go.Bar(
            x=covs, y=labels, orientation='h',
            marker_color=[C_GREEN if v >= 60 else (C_BLUE if v >= 25 else '#F59E0B') for v in covs],
            text=[f'{v:.0f}%' for v in covs], textposition='outside',
            hovertemplate='%{y}: %{x:.1f}% populated<extra></extra>',
        ))
        figq.update_layout(**base(''), height=300, showlegend=False,
                           xaxis=dict(range=[0, 108], showgrid=True, gridcolor=C_GREY, ticksuffix='%'),
                           yaxis=dict(showgrid=False))
        st.plotly_chart(figq, use_container_width=True)
        st.caption("Green ≥60% · blue ≥25% · amber <25%. Low-coverage fields are optional/discretionary "
                   "tags — a useful signal for filing-quality work, not gaps in the parser.")

    st.divider()
    st.subheader("Charity filings")

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
        'audit_status', 'auditor_name', 'city_or_town', 'postcode', 'charity_latest_income',
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
            help="Extracts auditor name, city/town, postcode, accounts type, audit status and principal activities from the raw iXBRL. Adds 6 extra columns but takes significantly longer."
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

                    # CIC filings are double-zipped and skipped by the core parser,
                    # so they aren't in df. Append the ones we recovered from nested
                    # zips as new rows (financial columns left blank) so they show up
                    # in coverage and regional analysis.
                    n_cic = 0
                    if 'is_cic' in extra.columns:
                        present = set(df['_company_number'])
                        cic_rows = extra[(extra['is_cic'] == True) &
                                         (~extra['_company_number'].isin(present))].copy()
                        if len(cic_rows) > 0:
                            cic_rows['company_id'] = cic_rows['_company_number']
                            if '_entity_name' in cic_rows.columns:
                                cic_rows['entity_current_legal_name'] = cic_rows['_entity_name']
                            df = pd.concat([df, cic_rows], ignore_index=True)
                            n_cic = len(cic_rows)

                    if 'is_cic' in df.columns:
                        df['is_cic'] = df['is_cic'].fillna(False).astype(bool)
                    df.drop(columns=['_company_number', '_entity_name'],
                            inplace=True, errors='ignore')

                    enrich_cols = ['city_or_town', 'auditor_name', 'audit_status',
                                   'accounts_type', 'principal_activities', 'postcode']
                    n_added = len([c for c in enrich_cols if c in df.columns])
                    st.success(f"✅ Enrichment complete — added {n_added} extra columns.")
                    if n_cic:
                        st.info(
                            f"➕ Recovered **{n_cic:,}** CIC filing(s) from nested zips that the "
                            "core parser skips. They appear as rows with identity + tagged "
                            "fields (incl. postcode); financial columns are blank, as the "
                            "double-zipped format isn't read by `stream_read_xbrl`."
                        )
                    if 'postcode' in df.columns:
                        n_pc = int(df['postcode'].notna().sum())
                        pc_cov = n_pc / len(df) * 100 if len(df) else 0
                        st.info(
                            f"📮 **Postcode coverage:** {pc_cov:.1f}% of filings had a "
                            f"registered-office postcode tagged ({n_pc:,} of {len(df):,}). "
                            "Note: this is the *tagged* address from the filing, not the "
                            "Companies House registered office — coverage reflects filers' "
                            "discretionary address tagging."
                        )
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
        "- **Extra enrichment** (optional): `auditor_name`, `city_or_town`, `postcode`, `audit_status`, `principal_activities`\n\n"
        "Parsing uses the UK government's open-source "
        "[stream-read-xbrl](https://github.com/uktrade/stream-read-xbrl) library."
    )
