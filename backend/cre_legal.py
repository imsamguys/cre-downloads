"""Plain-language legal and privacy pages for CRE's current local application."""
from __future__ import annotations

from datetime import date
import os

import streamlit as st


LEGAL_NAV = ["Privacy Policy", "Terms & Conditions", "Cookie Policy", "Refund Policy"]


def _identity() -> tuple[str, str, str]:
    return (
        os.environ.get("CRE_LEGAL_BUSINESS_NAME", "CRE operator"),
        os.environ.get("CRE_LEGAL_CONTACT_EMAIL", "Not configured"),
        os.environ.get("CRE_LEGAL_JURISDICTION", "Not configured"),
    )


def _header(title: str) -> None:
    business, email, _ = _identity()
    st.title(title)
    st.caption(f"Effective {date.today().isoformat()} · Operator: {business} · Contact: {email}")
    if email == "Not configured":
        st.warning("Set CRE_LEGAL_CONTACT_EMAIL and the business/jurisdiction environment settings before a public launch.")


def _privacy_preferences() -> None:
    st.subheader("Privacy preferences")
    st.write("Essential session storage is required for the controls and render queue to work.")
    st.checkbox("Essential session storage", value=True, disabled=True, key="privacy_essential")
    st.toggle(
        "Allow optional analytics if analytics are added later",
        value=False,
        key="privacy_optional_analytics",
        help="CRE currently has no analytics integration. Optional tracking must remain off unless you turn this on after one is added.",
    )
    st.caption("Optional analytics and advertising trackers are currently absent from this app.")


def render_legal_page(page: str) -> None:
    business, email, jurisdiction = _identity()
    if page == "Privacy Policy":
        _header("Privacy Policy")
        st.markdown(
            """
CRE processes the media, captions, edit instructions, and render settings you choose to provide. The current app stores these files in its configured local data folder. It does not sell personal data and does not include advertising or analytics trackers.

CRE sends content to an external AI provider only when you choose an AI planning or analysis action. The provider named beside that action receives the minimum input needed for the request. Uploaded media and generated files remain on the machine unless you explicitly use such a provider or a future publishing integration.

You may remove local source files, generated renders, and planning jobs from the app's data folder. Keep only media you are authorized to process. Contact the operator for access, correction, deletion, or privacy questions.

Security controls include restricted file types, server-side content checks, local-only service bindings, protected secrets, and validation of imported plans. No security control can remove every risk; report suspected exposure to the operator promptly.
"""
        )
        _privacy_preferences()
    elif page == "Terms & Conditions":
        _header("Terms & Conditions")
        st.markdown(
            f"""
By using CRE, you confirm that you have permission to upload, edit, and publish the media you provide. You are responsible for reviewing every generated edit, caption, claim, music track, and license before publication.

Do not use CRE to upload malware, unlawful content, private material without authorization, or content that infringes another person's rights. Do not bypass upload controls, access another user's records, tamper with protected fields, or misuse provider credentials.

The software is provided for editing assistance. Output may contain transcription, placement, or model errors and must be reviewed before use. Service availability may change as local tools and external providers change.

Operator: {business}. Governing jurisdiction: {jurisdiction}. Contact: {email}.
"""
        )
    elif page == "Cookie Policy":
        _header("Cookie Policy")
        st.markdown(
            """
CRE uses essential Streamlit session storage so navigation, controls, and queued work function during your session. The current application does not set advertising cookies and does not include analytics trackers.

Optional tracking must be disabled by default and may be enabled only after a clear purpose, provider, retention period, and opt-out control are added to this page. Embedded local editing tools receive only the short-lived bridge token needed to access the selected local editing session.

Browser settings can clear local site data. Blocking essential session storage may prevent the application from working correctly.
"""
        )
        _privacy_preferences()
    elif page == "Refund Policy":
        _header("Refund Policy")
        st.info("CRE does not currently accept payments or sell subscriptions, so there are no charges to refund.")
        st.markdown(
            """
Before paid plans launch, this policy must be updated with the seller's identity, prices, renewal and cancellation terms, refund window, delivery rules, statutory consumer rights, and payment-provider process. Payment handling must not launch until the published policy matches the checkout terms and signed webhook verification is active.
"""
        )
    else:
        raise ValueError("Unknown legal page")
