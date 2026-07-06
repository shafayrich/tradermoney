# Privacy Policy for TraderMoney

**Last Updated:** July 6, 2026

**Effective Date:** Upon Installation

---

## 1. Overview and Purpose

This Privacy Policy ("Policy") describes how TraderMoney ("Software," "we," "us," "our," or "Licensor") handles, stores, and processes data when you use the Software. 

**CRITICAL NOTE:** You are responsible for all data security, privacy compliance, and regulatory obligations associated with your use of the Software. This Policy is informational and does NOT guarantee that your use complies with applicable privacy laws.

---

## 2. Data We Collect and How

### 2.1 API Keys and Credentials
- **What we collect:** You enter API keys, authentication tokens, or login credentials for third-party services (brokers, market data providers, AI services).
- **Where stored:** API keys are stored locally on your device in encrypted configuration files:
  - Default location: `~/.tradermoney_config.enc`
  - Encryption method: AES encryption via the Python `cryptography` library
- **Who stores it:** YOU own and control this file. Licensor does NOT transmit, upload, or share your credentials with external servers.
- **Your responsibility:** You must protect this file. If compromised, revoke all API keys immediately.

### 2.2 Trading and Account Data
- **What we collect:** Order history, account balance, positions, performance metrics, backtest results, and other data retrieved from your broker or market data APIs.
- **Where stored:** Stored locally in an SQLite database on your device:
  - Default location: `~/.tradermoney_data.db`
- **Who stores it:** YOU own this file. Licensor does NOT transmit, upload, or share this data without your explicit action.
- **Your responsibility:** Secure this database file. Back it up regularly. If you delete it, historical data is lost.

### 2.3 Market and News Data
- **What we collect:** Market prices, historical data, news articles, and other information retrieved from third-party data providers (yfinance, NewsAPI, OpenRouter, etc.).
- **Where stored:** Cached locally in the database or in temporary memory during the session.
- **Third-party responsibility:** This data is governed by each provider's terms and privacy policy.
- **Your responsibility:** Ensure your use of such data complies with each provider's terms and any licensing requirements.

### 2.4 License Validation and Telemetry
- **What we collect:** License validation may transmit:
  - License key (anonymized or hashed, not the key itself)
  - Software version number
  - Operating system and system info (OS name, architecture)
  - Timestamp of validation check
  - Non-personally identifiable unique device ID (if enabled)
- **Why we collect it:** To verify that you have a valid license and to gather anonymized usage statistics.
- **Where it goes:** Transmitted to Licensor's license validation server (Gumroad or similar).
- **Opt-out:** Currently, license validation is mandatory if you use a paid license. You cannot opt out.
- **Retention:** Licensor retains validation logs for [30-90] days for security and auditing purposes.

### 2.5 Error Logs and Diagnostics
- **What we collect:** If enabled, error logs may capture:
  - Exception messages and stack traces
  - Browser console output
  - Non-sensitive system information
- **Where stored:** Locally in log files on your device (location: [configurable or default path])
- **Transmission:** Error logs are NOT automatically transmitted to Licensor unless you explicitly enable crash reporting.
- **Your responsibility:** Disable error reporting or audit log files if they may contain sensitive data.

### 2.6 What We DON'T Collect
- **Personal information:** We do not collect your name, email, address, phone number, or social security number (unless you voluntarily provide it in support requests).
- **Surveillance:** We do not track your browsing, trading patterns, investment decisions, or financial status.
- **Account credentials:** We do NOT access your broker accounts directly. You provide credentials; we store them locally.

---

## 3. How Your Data is Used

### 3.1 For Software Functionality
- Stored data enables the Software to function: displaying account balances, executing trades, backtesting strategies, and retrieving market data.

### 3.2 For License Validation
- License keys are validated to confirm you have an active, valid license.

### 3.3 For Improvements
- Anonymized, aggregated usage statistics may be used to improve the Software (e.g., identifying commonly-used features, detecting bugs).

### 3.4 For Legal and Compliance
- Licensor may use data to investigate violations of the EULA, respond to legal requests, or comply with law.

---

## 4. Third-Party Data Sharing and Processing

### 4.1 Third-Party Services
When you use the Software to connect to third-party services (brokers, APIs, data providers), you directly share data with those services:

- **Broker APIs (Alpaca, Interactive Brokers, Tradier, Binance, Bybit, OKX, etc.)**
  - You transmit API credentials to those brokers.
  - You send trade orders and account requests to those brokers.
  - Those brokers collect your trading activity, account information, and personal data per their own privacy policies.
  - Licensor does NOT control, monitor, or guarantee the security of broker-managed data.

- **Market Data Providers (yfinance, NewsAPI, etc.)**
  - Market data requests are sent to those providers' servers.
  - Those providers may collect usage data, request patterns, or IP information per their privacy policies.

- **AI Services (OpenRouter, etc.)**
  - If you enable AI analysis features, your queries and market data may be sent to AI service providers.
  - Those providers may use your data for model training, analytics, or other purposes per their terms.
  - Licensor does NOT control how AI providers use your data.

### 4.2 Your Responsibility
You are responsible for:
- Reading and understanding each third-party service's privacy policy.
- Complying with each service's data usage terms.
- Understanding the security and privacy risks of sending data to third parties.

---

## 5. Data Security and Encryption

### 5.1 Local Storage
- Credentials are encrypted locally using AES encryption.
- Database files are stored in plaintext (unencrypted) by default. You may enable OS-level encryption (macOS FileVault, Windows BitLocker, Linux LUKS) for additional protection.

### 5.2 Transmission
- API requests to third-party services use HTTPS/TLS encryption in transit.
- Licensor does NOT transmit sensitive data (API keys, credentials, account data) to Licensor servers.

### 5.3 Your Responsibility
- Secure your device with a strong password or biometric authentication.
- Enable full-disk encryption (FileVault, BitLocker, etc.).
- Keep your operating system and the Software updated with security patches.
- Monitor your encrypted config file and database for unauthorized access.
- Revoke API keys immediately if you suspect a compromise.

### 5.4 What Licensor Cannot Guarantee
- Licensor cannot guarantee that local encryption is unbreakable or that no data will be accessed if your device is compromised.
- Licensor is not responsible for data loss due to device failure, theft, malware, or user negligence.

---

## 6. Data Retention and Deletion

### 6.1 Local Data Retention
- Credential and trading data are retained indefinitely on your device unless you manually delete them.
- You may delete the encrypted config file or database at any time.
- Deletion is permanent and cannot be undone.

### 6.2 License Validation Logs
- Licensor retains license validation logs on its servers for [30-90] days.
- Logs are then permanently deleted or archived.

### 6.3 Error Logs
- Local error logs are retained until you manually delete them.
- Transmitted error reports (if enabled) may be retained by Licensor for up to 90 days.

### 6.4 Data Subject Rights (GDPR, CCPA, etc.)
If applicable law grants you specific rights (e.g., GDPR, CCPA), see Section 9 below.

---

## 7. Cookies and Tracking

The Software (desktop application) does NOT use cookies or traditional web tracking. However:
- If you use a web-based version of the Software, standard web analytics and cookies may apply.
- Third-party services (brokers, data providers) may use cookies and tracking per their policies.

---

## 8. Children's Privacy

This Software is not designed for children under 13 (or the applicable age of digital consent in your jurisdiction). We do not knowingly collect data from children. If you believe a child is using the Software, please contact us immediately.

---

## 9. International Data Protection and Regulatory Compliance

### 9.1 GDPR (General Data Protection Regulation) — EU
If you reside in the EU, GDPR grants you specific rights:

- **Right of Access:** You may request a copy of your data held by Licensor.
- **Right to Rectification:** You may request correction of inaccurate data.
- **Right to Erasure:** You may request deletion of your data ("right to be forgotten").
- **Right to Data Portability:** You may request your data in a portable format.
- **Right to Restrict Processing:** You may restrict how your data is used.
- **Right to Object:** You may object to certain processing.
- **Automated Decision-Making Rights:** You may request review of automated decisions.

**Exercise Your Rights:** Contact [INSERT CONTACT EMAIL] with your request. Provide sufficient information to identify your data. We will respond within 30 days (extendable to 90 days for complex requests).

**Note:** Most of your data is stored locally on your device, not on Licensor's servers. You control this data and can delete it directly. For data stored by Licensor (license validation logs), we will comply with GDPR requests.

### 9.2 CCPA (California Consumer Privacy Act) — USA (California)
If you reside in California, CCPA grants you similar rights:

- **Right to Know:** You may request what personal information we collect.
- **Right to Delete:** You may request deletion of your data.
- **Right to Opt-Out:** You may opt out of data sales (we do not sell data).
- **Right to Non-Discrimination:** We will not discriminate against you for exercising CCPA rights.

**Exercise Your Rights:** Contact [INSERT CONTACT EMAIL]. We will respond within 45 days.

### 9.3 Other Jurisdictions
If you reside in another jurisdiction with privacy laws (Canada, Australia, Brazil, etc.), similar rights may apply. Contact Licensor to inquire about your specific rights.

### 9.4 Licensor's Basis for Processing
Where applicable law requires it, Licensor's basis for processing your data is:
- **License Validation:** Legitimate business interest (anti-piracy, licensing compliance).
- **Error Logs:** Legitimate business interest (debugging, improving software).
- **Third-Party Requests:** Legal obligation (compliance with law enforcement requests).

---

## 10. Data Breaches and Incident Response

### 10.1 If Your Local Data is Compromised
If your device is hacked or your credential files are accessed:
1. Immediately revoke all API keys from your broker and service accounts.
2. Change passwords for all accounts.
3. Run antivirus/malware scans on your device.
4. Contact your brokers to report the unauthorized access.

### 10.2 If Licensor Experiences a Data Breach
If Licensor becomes aware of a breach affecting data stored on our servers (e.g., license validation logs):
- Licensor will notify you within [30-60] days.
- Licensor will disclose the scope of the breach and measures taken to secure data.
- Licensor will not be liable for breaches caused by your negligence or third-party intrusions beyond reasonable security measures.

---

## 11. Cross-Border Data Transfers

If Licensor's servers or license validation services are located outside your jurisdiction:
- Your data may be transferred, stored, or processed internationally.
- Licensor will comply with applicable data transfer regulations (e.g., GDPR Standard Contractual Clauses).
- You consent to such transfers by using the Software.

---

## 12. Third-Party Links and Websites

The Software or documentation may link to third-party websites (broker sites, documentation, etc.). Licensor is not responsible for the privacy practices of third-party websites. Read their privacy policies before providing data to them.

---

## 13. Your Responsibilities and Security Best Practices

### 13.1 You Must
- Store your API keys and credentials securely.
- Enable device encryption (FileVault, BitLocker, etc.).
- Keep your operating system and software updated.
- Use strong, unique passwords.
- Monitor your accounts for unauthorized activity.
- Read and comply with the terms of service of all third-party services.

### 13.2 You Must NOT
- Commit API keys or credentials to public source code repositories.
- Share your credentials or encrypted config files with others.
- Use the Software on shared or unsecured devices.
- Ignore warnings about missing permissions or API key errors.

---

## 14. Contact and Questions

For questions about this Privacy Policy, to exercise your privacy rights, or to report a data breach:

**Email:** [INSERT CONTACT EMAIL]
**Mailing Address:** [INSERT MAILING ADDRESS]
**Phone:** [INSERT PHONE NUMBER, if applicable]

**Response Time:** Licensor will respond to privacy inquiries within 30 days where legally required.

---

## 15. Changes to This Privacy Policy

Licensor may update this Privacy Policy at any time. Changes become effective when posted. Your continued use of the Software constitutes acceptance of the updated Policy. 

For significant changes, Licensor will provide notice when reasonably practicable.

**Last Updated:** June 15, 2026

---

## 16. No Privacy Guarantee; Consult a Professional

**THIS PRIVACY POLICY IS INFORMATIONAL ONLY. IT DOES NOT GUARANTEE THAT YOUR USE OF THE SOFTWARE COMPLIES WITH APPLICABLE PRIVACY LAWS, INCLUDING GDPR, CCPA, OR OTHER REGULATIONS.**

**IF YOU HANDLE PERSONAL DATA, PROCESS DATA FROM EUROPEAN RESIDENTS, OR OPERATE IN A REGULATED INDUSTRY, CONSULT A DATA PRIVACY PROFESSIONAL OR ATTORNEY TO ENSURE COMPLIANCE WITH APPLICABLE LAWS BEFORE USING THE SOFTWARE COMMERCIALLY.**

**LICENSOR ASSUMES NO RESPONSIBILITY FOR REGULATORY FINES, DATA BREACH LIABILITIES, OR LEGAL CONSEQUENCES OF YOUR DATA HANDLING PRACTICES.**

---

**End of Privacy Policy**
