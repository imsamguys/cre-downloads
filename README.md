# CRE local engine

Download this repository with **Code → Download ZIP**. It contains the local Python engine for [CRE Studio](https://cre-cre2.vercel.app/studio/) and the Streamlit editor for Formats 1 and 2. Source media and rendered clips stay on the computer running the engine.

## Start the Studio companion

Install Python 3.13+, FFmpeg, and FFprobe. From `backend/`, install the dependencies:

```sh
python -m venv .venv
python -m pip install -r requirements-local-api.txt
```

Activate `.venv`, then run `start-local.ps1` on Windows or `./start-local.sh` on macOS/Linux. The companion listens only on `127.0.0.1:8788`. Open [Studio](https://cre-cre2.vercel.app/studio/) and sign in with your CRE account. For the Formats 1 and 2 Streamlit editor, run `start_cre.ps1` on Windows or `python -m streamlit run app.py` from `backend/` on macOS/Linux.

The included `.env.example` contains only a public Supabase URL and publishable key. Never put provider secrets, service role keys, account tokens, or personal media in this repository. Optional local integrations may require credentials in your own ignored `.streamlit/secrets.toml`.

This download tracks the released local engine. Changes to the [protected staging site](https://cre-git-stage-cre2.vercel.app/) are work in progress and may require a matching development checkout to test. Check the latest release before installing an update.

For installation problems or account deletion, open an issue here without posting account details or media; request a private contact route. Security reports should use this repository's private security advisory feature.

Code is MIT licensed. The bundled Montserrat font uses SIL OFL 1.1; see `backend/assets/fonts/OFL-Montserrat.txt`.
