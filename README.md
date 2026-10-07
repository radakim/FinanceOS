# FinanceOS – Personal Finance Pro

A private, multi-user personal finance app built with **Streamlit** and **PostgreSQL (Neon.tech)**.

## Features
- 💰 Dual currency: USD & KHR (Cambodian Riel)
- 📊 Dashboard with live analytics and forecasting
- 📝 Daily expense/income entry + quick expense button
- 📋 Transaction monitoring with edit/delete
- 🎯 Monthly budget planner (% or real amount)
- 👥 Multi-user authentication with per-user data isolation
- 🔒 Powered by Neon PostgreSQL (cloud)

## Deploy to Streamlit Cloud

1. Fork/upload this repo to GitHub (private recommended)
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect your GitHub repo → `app.py`
4. In **Advanced Settings → Secrets**, add:
   ```toml
   DATABASE_URL = "postgresql://neondb_owner:PASSWORD@ep-xxx.region.aws.neon.tech/neondb?sslmode=require"
   ```
5. Click **Deploy**

## Default Login
- Username: `admin`
- Password: `admin123`
- Recovery PIN: `1234`

## Local Development
```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql://..."
streamlit run app.py
```
