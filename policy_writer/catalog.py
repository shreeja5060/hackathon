"""What a starter policy is built from: topics, the NIST controls behind them, and baseline clauses.

Every clause is tied to one NIST SP 800-53 Rev. 5 control. The controls come from the
SP 800-53B LOW baseline (the set NIST defines for low-impact systems, a fit for small
organizations), plus a few MODERATE-baseline controls that small businesses are
commonly told to adopt anyway (device lock, disk encryption, least privilege, spam
filtering). Baseline membership was checked against NIST's OSCAL profiles, release
5.2.0, the same release Phase 1 indexes:

    https://github.com/usnistgov/oscal-content  nist.gov/SP800-53/rev5/json/
        NIST_SP-800-53_rev5_LOW-baseline_profile.json
        NIST_SP-800-53_rev5_MODERATE-baseline_profile.json

Titles are NIST's. The clauses are ours: plain-English starting points with specific,
checkable values, written for an organization of 1 to 250 people. In live mode the
writer gives them to Claude together with NIST's control text and the organization's
answers, and Claude adapts them; in the simulator they are used as they are.

Placeholders in clause text (filled from the organization profile):
    {org}     organization name          {lead}   "the Security Lead"
    {it}      who does the IT work       {review} how often accounts are reviewed
    {apps}    the business apps in use   {restore} how often restores are tested
"""

from __future__ import annotations

from dataclasses import dataclass, field

# -------------------------------------------------------------------- controls

# id -> (official title, baseline). "low" = SP 800-53B LOW baseline; "moderate" = added from MODERATE.
CONTROLS: dict[str, tuple[str, str]] = {
    "PL-4": ("Rules of Behavior", "low"),
    "PL-4(1)": ("Social Media and External Site/Application Usage Restrictions", "low"),
    "AC-2": ("Account Management", "low"),
    "AC-6": ("Least Privilege", "moderate"),
    "IA-2(1)": ("Multi-factor Authentication to Privileged Accounts", "low"),
    "IA-2(2)": ("Multi-factor Authentication to Non-privileged Accounts", "low"),
    "IA-5": ("Authenticator Management", "low"),
    "IA-5(1)": ("Password-based Authentication", "low"),
    "AC-7": ("Unsuccessful Logon Attempts", "low"),
    "AC-11": ("Device Lock", "moderate"),
    "AC-19": ("Access Control for Mobile Devices", "low"),
    "SI-3": ("Malicious Code Protection", "low"),
    "SI-2": ("Flaw Remediation", "low"),
    "CM-11": ("User-installed Software", "low"),
    "CM-8": ("System Component Inventory", "low"),
    "SC-28": ("Protection of Information at Rest", "moderate"),
    "SI-8": ("Spam Protection", "moderate"),
    "AT-2(3)": ("Social Engineering and Mining", "moderate"),
    "SC-7": ("Boundary Protection", "low"),
    "AC-17": ("Remote Access", "low"),
    "AC-18": ("Wireless Access", "low"),
    "AC-20": ("Use of External Systems", "low"),
    "CP-9": ("System Backup", "low"),
    "CP-9(1)": ("Testing for Reliability and Integrity", "moderate"),
    "CP-10": ("System Recovery and Reconstitution", "low"),
    "SC-13": ("Cryptographic Protection", "low"),
    "MP-6": ("Media Sanitization", "low"),
    "MP-7": ("Media Use", "low"),
    "IR-6": ("Incident Reporting", "low"),
    "IR-4": ("Incident Handling", "low"),
    "IR-8": ("Incident Response Plan", "low"),
    "AT-2": ("Literacy Training and Awareness", "low"),
    "AT-3": ("Role-based Training", "low"),
    "SA-9": ("External System Services", "low"),
    "PS-7": ("External Personnel Security", "low"),
    "PS-6": ("Access Agreements", "low"),
    "PS-4": ("Personnel Termination", "low"),
    "PS-5": ("Personnel Transfer", "low"),
}

FRAMEWORK = "NIST SP 800-53 Rev. 5 (release 5.2.0), SP 800-53B low baseline plus selected moderate controls"


def base_control(control_id: str) -> str:
    """IA-2(1) -> IA-2. Used to tell whether a policy addresses a control family member at all."""
    return control_id.split("(", 1)[0]


# -------------------------------------------------------------------- the organization

SIZES = ("1-10", "11-50", "51-250")
SIZE_LABELS = {"1-10": "1 to 10 people", "11-50": "11 to 50 people", "51-250": "51 to 250 people"}
IT_SUPPORT = {
    "owner": "The owner or a manager does it",
    "in_house": "One or more in-house IT staff",
    "provider": "An outside IT provider",
}
SECTORS = ("Professional services", "Retail or e-commerce", "Healthcare", "Manufacturing", "Construction",
           "Hospitality", "Non-profit", "Education", "Other")
USES = {
    "cloud_office": "Email and office apps in the cloud (Microsoft 365, Google Workspace)",
    "laptops": "Company laptops or desktops",
    "personal_phones": "Staff use personal phones for work",
    "remote_work": "People work from home or travel",
    "customer_data": "We keep customers' personal data",
    "card_payments": "We take card payments",
    "office_wifi": "An office network or Wi-Fi",
    "website": "A website or online store",
    "vendors": "Outside companies handle our data",
}


@dataclass(frozen=True)
class Profile:
    org: str
    size: str = "1-10"
    sector: str = "Other"
    it_support: str = "owner"
    uses: frozenset = field(default_factory=frozenset)

    def has(self, flag: str) -> bool:
        return flag in self.uses


# -------------------------------------------------------------------- topics

@dataclass(frozen=True)
class Clause:
    control: str
    text: str
    when: str | None = None  # a USES flag (or "it:provider" etc.) that must hold for the clause to apply


@dataclass(frozen=True)
class Topic:
    id: str
    title: str
    summary: str  # one line for the picker
    clauses: tuple[Clause, ...]
    always: bool = True  # recommended for every organization
    recommended_if: tuple[str, ...] = ()  # USES flags (or "it:provider") that make it recommended

    @property
    def controls(self) -> list[str]:
        seen: list[str] = []
        for clause in self.clauses:
            if clause.control not in seen:
                seen.append(clause.control)
        return seen


TOPICS: tuple[Topic, ...] = (
    Topic("acceptable_use", "Acceptable use", "What company computers, accounts and data may be used for.", (
        Clause("PL-4", "Everyone who uses {org}'s computers, accounts or data must read this policy and confirm "
                       "in writing that they will follow it before they get access, and again within 30 days "
                       "of any change to it."),
        Clause("PL-4", "Company systems are for business use. Limited personal use is allowed when it does not "
                       "interfere with work, break the law or put company data at risk."),
        Clause("PL-4(1)", "Staff must not post company or customer information on social media, or upload it to "
                          "websites and apps that {lead} has not approved."),
    )),
    Topic("accounts", "Accounts and access", "Who gets which accounts, and how access is kept to what each job needs.", (
        Clause("AC-2", "Every person must have their own account. Shared or generic logins are not allowed unless "
                       "{lead} approves them in writing and records who uses them."),
        Clause("AC-2", "Accounts must be created, changed or removed only with {lead}'s approval, and {it} must "
                       "review every account {review}, removing any that are no longer needed."),
        Clause("AC-6", "People must get only the access their job needs. Administrator rights are limited to {it} "
                       "and must not be used for everyday work such as email and web browsing."),
    )),
    Topic("sign_in", "Passwords and sign-in", "Multi-factor authentication, password rules and lockouts.", (
        Clause("IA-2(1)", "Multi-factor authentication (a code from an app or a security key, not only a "
                          "password) must be turned on for every administrator account."),
        Clause("IA-2(2)", "Multi-factor authentication must also be turned on for every user account on {apps}."),
        Clause("IA-5(1)", "Passwords must be at least 14 characters long. Passphrases made of several random words "
                          "are encouraged."),
        Clause("IA-5", "Passwords must not be reused across accounts or shared with anyone, including {it}. Staff "
                       "must keep them in the password manager that {lead} provides."),
        Clause("IA-5(1)", "A password must be changed immediately if there is any sign it has been exposed. "
                          "Routine forced password changes are not required."),
        Clause("AC-7", "Systems must lock an account for at least 15 minutes after 10 failed sign-in attempts "
                       "in a row."),
    )),
    Topic("devices", "Laptops, phones and software", "Screen locks, encryption, updates, anti-malware and apps.", (
        Clause("AC-11", "Computers must lock the screen after 15 minutes without use, and phones after 2 minutes. "
                        "Unlocking must need the password, PIN or fingerprint."),
        Clause("SC-28", "Full-disk encryption (BitLocker on Windows, FileVault on Mac) must be turned on for every "
                        "company laptop.", when="laptops"),
        Clause("SI-3", "Every company computer must run anti-malware protection that updates automatically and "
                       "scans files when they are downloaded or opened."),
        Clause("SI-2", "Security updates for operating systems, browsers and business apps must be installed "
                       "within 14 days of release, and within 2 days when the vendor says the flaw is being "
                       "actively exploited."),
        Clause("SI-2", "The same update deadlines apply to the website and its plugins.", when="website"),
        Clause("CM-11", "Only {it} may install software on company computers, and only apps on the approved list "
                        "that {lead} keeps."),
        Clause("CM-8", "{it} must keep a list of every company laptop, phone and online service with the person who "
                       "uses it, and update it whenever equipment is bought, reassigned or retired."),
        Clause("AC-19", "Phones and tablets that open company email or files must have a screen lock and current "
                        "software, and must be enrolled so that {it} can remove company data if a device is lost."),
        Clause("AC-19", "Personal phones may be used for work only on these terms. {it} will remove only company "
                        "data from a personal phone, never personal data.", when="personal_phones"),
    )),
    Topic("email_internet", "Email and internet", "Spam and phishing filters, payment fraud and the office network.", (
        Clause("SI-8", "The spam, phishing and malware filters of our email service must stay turned on for every "
                       "mailbox."),
        Clause("AT-2(3)", "A request to change bank details or to make an urgent payment must be confirmed by "
                          "phone, using a number already on file, before any money is sent."),
        Clause("SC-7", "The office internet connection must be protected by a firewall, with remote management "
                       "switched off and the router's default administrator password changed.", when="office_wifi"),
    )),
    Topic("remote_work", "Working remotely", "Remote access, Wi-Fi and devices outside the office.", (
        Clause("AC-17", "Company systems that are not cloud services must be reached from outside the office only "
                        "through the company VPN, with multi-factor authentication."),
        Clause("AC-17", "Staff must not use public Wi-Fi for work unless they connect through the company VPN or "
                        "their phone's hotspot."),
        Clause("AC-18", "Office Wi-Fi must use WPA2 or WPA3 encryption with a strong passphrase, and visitors must "
                        "use a separate guest network.", when="office_wifi"),
        Clause("AC-20", "Company data must not be opened on shared or public computers, and personal computers "
                        "may be used for company work only with {lead}'s written approval."),
    ), always=False, recommended_if=("remote_work", "personal_phones")),
    Topic("data_backups", "Protecting data and backups", "Backups, restores, encryption, removable media and disposal.", (
        Clause("CP-9", "Important data must be backed up automatically at least daily, with at least one copy kept "
                       "off-site or in a separate account that everyday user accounts cannot delete."),
        Clause("CP-9", "Data kept in cloud services, such as email and shared files, must be backed up too, because "
                       "deleted or encrypted files sync everywhere.", when="cloud_office"),
        Clause("CP-9(1)", "{it} must test restoring files from backup {restore} and record the result."),
        Clause("CP-10", "{it} must be able to restore critical systems from backup within 2 business days of an "
                        "outage."),
        Clause("SC-13", "Customer personal data must be encrypted when it is stored and when it is sent outside "
                        "{org}, using the encryption built into our approved services.", when="customer_data"),
        Clause("MP-7", "USB drives and other removable storage must not be used for company data unless {lead} "
                       "approves it and the drive is encrypted."),
        Clause("MP-6", "Before a computer, phone or drive is sold, recycled or returned, {it} must securely wipe or "
                       "destroy its storage and record that this was done."),
    )),
    Topic("incidents", "Security incidents", "Reporting, handling and planning for incidents.", (
        Clause("IR-6", "Anyone who suspects a security incident, such as clicking a suspicious link, losing a "
                       "device, seeing a virus warning or sending data to the wrong person, must report it to "
                       "{lead} immediately and in any case within 1 hour."),
        Clause("IR-4", "{lead} must coordinate the response: contain the problem (for example, disconnect the device "
                       "or reset the password), fix the cause, restore from backup if needed, and record what "
                       "happened and what will change."),
        Clause("IR-8", "{lead} must keep a one-page incident response plan with contact details for {it}, our bank "
                       "and our insurer, and review it every year."),
        Clause("IR-8", "If personal data may have been exposed, {lead} must check which legal notification "
                       "deadlines apply and notify affected people and regulators where required.",
               when="customer_data"),
    )),
    Topic("training", "Security awareness training", "What everyone learns, and when.", (
        Clause("AT-2", "Everyone must complete security awareness training, including how to spot phishing, within "
                       "30 days of joining and every year after that."),
        Clause("AT-3", "{it} and anyone with administrator rights must complete extra training on their security "
                       "duties every year."),
    )),
    Topic("vendors", "IT providers and outside services", "Choosing and checking the companies that handle our data.", (
        Clause("SA-9", "Before a new online service or IT provider is used for company or customer data, {lead} must "
                       "check that it offers multi-factor authentication, encryption and a commitment to report "
                       "breaches, and add it to the list of approved services."),
        Clause("SA-9", "{lead} must review each provider's security at least once a year."),
        Clause("SA-9", "Card payments must be taken only through our payment provider's terminal or website, so "
                       "that {org} never writes down, emails or stores full card numbers.", when="card_payments"),
        Clause("PS-7", "Contractors and IT providers must follow this policy, use named accounts with "
                       "multi-factor authentication, and lose their access as soon as their work ends."),
    ), always=False, recommended_if=("vendors", "cloud_office", "card_payments", "it:provider")),
    Topic("people", "Joining, moving and leaving", "Access when people start, change roles or leave.", (
        Clause("PS-6", "New staff and contractors must sign the acceptable use agreement before they get any "
                       "account."),
        Clause("PS-5", "When someone changes role, {it} must adjust their access within 5 working days so they "
                       "keep only what the new role needs."),
        Clause("PS-4", "When someone leaves, {it} must disable all their accounts and collect company devices on "
                       "their last day, and change shared secrets they knew, such as the Wi-Fi passphrase, within "
                       "1 working day."),
    )),
)

TOPIC_BY_ID = {topic.id: topic for topic in TOPICS}


def recommended_topics(profile: Profile) -> list[str]:
    picked = []
    for topic in TOPICS:
        flags = [flag for flag in topic.recommended_if
                 if (flag == f"it:{profile.it_support}") or profile.has(flag)]
        if topic.always or flags:
            picked.append(topic.id)
    return picked


def topics_for_controls(control_ids) -> list[str]:
    """The topics whose clauses implement any of these controls (matching on the base control)."""
    wanted = {base_control(c) for c in control_ids}
    return [topic.id for topic in TOPICS if any(base_control(c) in wanted for c in topic.controls)]


def starter_controls() -> list[str]:
    """Every control a full starter policy implements, in topic order."""
    seen: list[str] = []
    for topic in TOPICS:
        for control in topic.controls:
            if control not in seen:
                seen.append(control)
    return seen
