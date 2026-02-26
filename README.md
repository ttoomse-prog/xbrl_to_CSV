# Companies House XBRL → CSV

A one-page Streamlit app that converts a Companies House bulk accounts ZIP file into a clean CSV.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open http://localhost:8501 in your browser.

## Usage

1. Download a bulk accounts ZIP from http://download.companieshouse.gov.uk/en_accountsdata.html
2. Upload it in the app
3. Preview the parsed dataframe (38 columns, one row per financial fact)
4. Optionally filter by company number or require certain fields to be non-null
5. Download the CSV

## Deploying publicly (optional)

The easiest option is [Streamlit Community Cloud](https://streamlit.io/cloud):
1. Push this folder to a GitHub repo
2. Connect it at share.streamlit.io
3. Share the URL

Note: files up to ~200 MB can be uploaded via Streamlit Cloud. For larger files, run locally.

## Data source

[Companies House bulk accounts data](http://download.companieshouse.gov.uk/en_accountsdata.html) — Open Government Licence v3.0  
Parsing by [stream-read-xbrl](https://github.com/uktrade/stream-read-xbrl) (UK Department for Business and Trade)
