# Fonts

Served by Streamlit's static file serving (`server.enableStaticServing`) and
registered in `../../.streamlit/config.toml`, so the dashboard looks the same
offline, on Cloud Run and on every laptop.

- **Public Sans** (400, 500, 600, 700): the U.S. Web Design System's typeface, used for all text.
- **IBM Plex Mono** (400, 500): control and finding IDs.

Both are under the SIL Open Font License 1.1 (`OFL-*.txt`). Latin subsets,
taken from the Fontsource packages (`@fontsource/public-sans`, `@fontsource/ibm-plex-mono`, 5.2.6).
