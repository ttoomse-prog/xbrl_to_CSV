import multiprocessing
multiprocessing.set_start_method('fork', force=True)  # needed for stream-read-xbrl on some platforms

import streamlit as st
import pandas as pd
import io
from stream_read_xbrl import stream_read_xbrl_zip

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Companies House XBRL → CSV",
    page_icon="🏢",
    layout="wide",
)

# ── Header ────────────────────────────────────────────────────────────────────
st.title("🏢 Companies House XBRL → CSV")
st.markdown(
    "Upload a bulk accounts ZIP file downloaded from "
    "[Companies House](http://download.companieshouse.gov.uk/en_accountsdata.html) "
    "and convert it to a clean CSV dataframe in seconds."
)

st.info(
    "**How to get the file:** Go to the Companies House bulk data page, download any "
    "`Accounts_Bulk_Data-YYYY-MM-DD.zip` file, then upload it here.",
    icon="ℹ️",
)

# ── File uploader ─────────────────────────────────────────────────────────────
uploaded = st.file_uploader(
    "Upload Companies House bulk accounts ZIP",
    type=["zip"],
    help="Files are typically 50–300 MB. Processing takes around 10–30 seconds.",
)

if uploaded is not None:
    filename = uploaded.name
    # Derive a default output filename from the input
    csv_name = filename.replace(".zip", ".csv").replace(".ZIP", ".csv")

    st.write(f"**File:** `{filename}`  |  **Size:** {uploaded.size / 1_048_576:.1f} MB")

    with st.spinner("Parsing XBRL data — this usually takes 10–30 seconds…"):
        try:
            # stream_read_xbrl_zip needs an iterable of bytes chunks
            file_bytes = uploaded.read()

            def byte_chunks(data, chunk_size=65_536):
                for i in range(0, len(data), chunk_size):
                    yield data[i : i + chunk_size]

            with stream_read_xbrl_zip(byte_chunks(file_bytes)) as (columns, rows):
                df = pd.DataFrame(rows, columns=columns)

        except Exception as e:
            st.error(f"**Error during parsing:** {e}")
            st.stop()

    st.success(f"✅ Done! Parsed **{len(df):,} rows** and **{len(df.columns)} columns**.")

    # ── Preview ───────────────────────────────────────────────────────────────
    st.subheader("Preview (first 100 rows)")
    st.dataframe(df.head(100), use_container_width=True)

    # ── Column summary ────────────────────────────────────────────────────────
    with st.expander("📋 Column summary"):
        summary = pd.DataFrame({
            "column": df.columns,
            "dtype": df.dtypes.values,
            "non_null": df.notna().sum().values,
            "null_%": (df.isna().mean() * 100).round(1).values,
            "sample": [df[c].dropna().iloc[0] if df[c].notna().any() else "" for c in df.columns],
        })
        st.dataframe(summary, use_container_width=True, hide_index=True)

    # ── Optional filters ──────────────────────────────────────────────────────
    with st.expander("🔍 Filter before downloading (optional)"):
        col1, col2 = st.columns(2)

        with col1:
            if "company_id" in df.columns:
                company_filter = st.text_input(
                    "Filter by company number(s)",
                    placeholder="e.g. 00012345, 00067890 (comma-separated)",
                )
            else:
                company_filter = ""

        with col2:
            numeric_cols = df.select_dtypes(include="number").columns.tolist()
            if numeric_cols:
                keep_non_null = st.multiselect(
                    "Only keep rows where these columns are non-null",
                    options=numeric_cols,
                    default=[],
                )
            else:
                keep_non_null = []

        filtered_df = df.copy()

        if company_filter.strip():
            ids = [c.strip() for c in company_filter.split(",") if c.strip()]
            filtered_df = filtered_df[filtered_df["company_id"].astype(str).isin(ids)]
            st.write(f"Filtered to **{len(filtered_df):,} rows** matching {len(ids)} company number(s).")

        for col in keep_non_null:
            filtered_df = filtered_df[filtered_df[col].notna()]
        if keep_non_null:
            st.write(f"After non-null filter: **{len(filtered_df):,} rows**.")

    # ── Download ──────────────────────────────────────────────────────────────
    st.subheader("Download")

    export_df = filtered_df if (company_filter.strip() or keep_non_null) else df

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
        "The output is a **38-column dataframe** with one row per tagged financial fact, including:\n\n"
        "- `company_id`, `company_name`, `balance_sheet_date`\n"
        "- Financial figures: `turnover`, `net_assets`, `current_assets`, `cash`, `employees`\n"
        "- Filing metadata: `accounts_type`, `period_start`, `period_end`\n"
        "- And 30+ more structured fields\n\n"
        "Parsing uses the UK government's open-source "
        "[stream-read-xbrl](https://github.com/uktrade/stream-read-xbrl) library."
    )
