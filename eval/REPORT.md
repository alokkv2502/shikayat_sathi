# Shikayat Saathi — evaluation (2026-10-09T06:41:31+00:00)

## A. Grievance contacts (grievance + verifier subgraph)

_Not run (no SerpApi key / cache, or --skip-contacts)._

## B. Complaints (intake → RAG → drafter → critic, search off)

| Metric | Value |
|---|---|
| Complaints | 16 (incl. Hindi & Hinglish) |
| Sector accuracy | 100.0% |
| Language detection | 100.0% |
| Drafts passing the critic | 100.0% |
| Final letters with zero uncited claims | 100.0% |
| Specificity: case-specific facts used per letter | 0.7 of 0.7 available |
| Correct legal frame (scheme → no consumer law; consumer → own sector's law) | 100.0% |
| Template overlap: avg max share of 5-word phrases shared with another letter (lower is better) | 65.8% |

| # | Lang | Expected | Got | Critic | Drafts | Facts used | Frame | Overlap |
|---|---|---|---|---|---|---|---|---|
| 1 | en | ecommerce | ecommerce ✔ | pass | 1 | 2/2 | ✔ | 66.7% |
| 2 | hinglish | ecommerce | ecommerce ✔ | pass | 1 | 1/1 | ✔ | 61.1% |
| 3 | en | ecommerce | ecommerce ✔ | pass | 1 | 0/0 | ✔ | 83.2% |
| 4 | en | ecommerce | ecommerce ✔ | pass | 1 | 0/0 | ✔ | 80.6% |
| 5 | en | bank | bank ✔ | pass | 1 | 1/1 | ✔ | 63.3% |
| 6 | hi | bank | bank ✔ | pass | 1 | 0/0 | ✔ | 57.6% |
| 7 | en | telecom | telecom ✔ | pass | 1 | 1/1 | ✔ | 69.0% |
| 8 | en | telecom | telecom ✔ | pass | 1 | 0/0 | ✔ | 69.7% |
| 9 | en | insurance | insurance ✔ | pass | 1 | 1/1 | ✔ | 63.7% |
| 10 | hinglish | insurance | insurance ✔ | pass | 1 | 1/1 | ✔ | 63.7% |
| 11 | en | loan_app | loan_app ✔ | pass | 1 | 0/0 | ✔ | 71.3% |
| 12 | hi | loan_app | loan_app ✔ | pass | 1 | 0/0 | ✔ | 66.7% |
| 13 | en | other | other ✔ | pass | 1 | 1/1 | ✔ | 61.9% |
| 14 | en | other | other ✔ | pass | 1 | 1/1 | ✔ | 68.2% |
| 15 | en | govt_scheme | govt_scheme ✔ | pass | 1 | 2/2 | ✔ | 46.3% |
| 16 | hinglish | govt_scheme | govt_scheme ✔ | pass | 1 | 0/0 | ✔ | 60.6% |

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
