# B2B Invoice Digitization & Accounting Import

I run a B2B reseller. Supplier invoices used to be read and typed by hand before anything could
be sold. I wanted that to happen by itself, with people only where their judgment matters.
This is that tool.

## What it does

- Picks up supplier invoices from email, or you drag a PDF into the window.
- Reads them with AI into structured lines and filters out documents that are not invoices.
- Lets people check, adjust and price the lines in the browser, with suggestions from the
  product catalog and memory of what they chose before.
- Sends the result into the accounting system automatically and shows the outcome next to
  the invoice.

## Principles

- Money is exact to the cent. The paper document is the anchor, not a formula.
- Nothing gets entered twice.
- Every automatic step can be switched off and done by hand.
- The tool remembers choices so people do not repeat them.

## Code

- `server.py` — the server and the API.
- `ekstrakcija.py`, `klasifikacija.py` — reading documents with AI.
- `paieska_engine.py`, `atmintis.py` — product catalog search and remembered choices.
- `xml_generavimas.py`, `pragma_*.py` — export to the accounting system and feedback from it.
- `pastas.py` — mail intake.
- `saskaitos.py`, `paskyros.py`, `prisijungimas.py`, `aplinka.py` — records, accounts, sessions.
- `static/` — the browser UI.

The UI and the comments are in Lithuanian. That is the language the people using it speak.

## Running it

```
python -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill in the keys
venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8300
```
