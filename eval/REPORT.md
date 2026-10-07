# Shikayat Saathi — evaluation (2026-10-07T11:05:52+00:00)

## A. Grievance contacts (grievance + verifier subgraph)

_Not run (no SerpApi key / cache, or --skip-contacts)._

## B. Complaints (intake → RAG → drafter → critic, search off)

| Metric | Value |
|---|---|
| Complaints | 14 (incl. Hindi & Hinglish) |
| Sector accuracy | 100.0% |
| Language detection | 100.0% |
| Drafts passing the critic | 100.0% |
| Final letters with zero uncited claims | 100.0% |

| # | Lang | Expected | Got | Critic | Drafts |
|---|---|---|---|---|---|
| 1 | en | ecommerce | ecommerce ✔ | pass | 1 |
| 2 | hinglish | ecommerce | ecommerce ✔ | pass | 1 |
| 3 | en | ecommerce | ecommerce ✔ | pass | 1 |
| 4 | en | ecommerce | ecommerce ✔ | pass | 1 |
| 5 | en | bank | bank ✔ | pass | 1 |
| 6 | hi | bank | bank ✔ | pass | 1 |
| 7 | en | telecom | telecom ✔ | pass | 1 |
| 8 | en | telecom | telecom ✔ | pass | 1 |
| 9 | en | insurance | insurance ✔ | pass | 1 |
| 10 | hinglish | insurance | insurance ✔ | pass | 1 |
| 11 | en | loan_app | loan_app ✔ | pass | 1 |
| 12 | hi | loan_app | loan_app ✔ | pass | 1 |
| 13 | en | other | other ✔ | pass | 1 |
| 14 | en | other | other ✔ | pass | 1 |

## C. Decoy test (synthetic scam patterns)

Decoys rejected **11/11** · genuine kept **5/5**

| Company | Contact | Pattern | Expected | Got |
|---|---|---|---|---|
| Flipkart | `grievance.officer@flipkart.com` | genuine | verified | ✔ verified |
| Flipkart | `9876500011` | look-alike helpline domain | unverified | ✔ unverified |
| Flipkart | `flipkart.refund.desk@gmail.com` | webmail on look-alike domain | unverified | ✔ unverified |
| Flipkart | `8800012345` | single scraped directory | unverified | ✔ unverified |
| Axis Bank | `pno@axis.bank.in` | genuine | verified | ✔ verified |
| Axis Bank | `axisbank.kyc.update@outlook.com` | KYC-update phishing | unverified | ✔ unverified |
| Axis Bank | `7000098765` | look-alike helpline domain | unverified | ✔ unverified |
| ICICI Bank | `pno@icicibank.com` | genuine | verified | ✔ verified |
| ICICI Bank | `9123400001` | look-alike helpline domain | unverified | ✔ unverified |
| ICICI Bank | `icici.grievance.cell@yahoo.co.in` | webmail seen on two forums | unverified | ✔ unverified |
| KreditBee | `help@kreditbee.in` | genuine | verified | ✔ verified |
| KreditBee | `9988776655` | fake loan-app helpline | unverified | ✔ unverified |
| KreditBee | `kreditbee.recovery.team@gmail.com` | fake recovery-team webmail | unverified | ✔ unverified |
| Zomato | `grievance@zomato.com` | genuine | verified | ✔ verified |
| Zomato | `9000011111` | look-alike helpline domain | unverified | ✔ unverified |
| Zomato | `help@zomato-support.example` | single source, non-official email domain | unverified | ✔ unverified |
