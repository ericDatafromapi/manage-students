import streamlit as st
import csv
import io
import json
import os
from datetime import date
from google.cloud import bigquery, resourcemanager_v3
from google.iam.v1 import iam_policy_pb2, policy_pb2
from google.oauth2 import service_account

# --- Config ---
# Cours en cours : à changer d'une promotion à l'autre, c'est ce qui s'affiche partout.
COURS = {
    "nom": "Social Data & Social Listening",
    "programme": "MSc Data Management DM2 — Aivancity",
    "dates": "31 août au 3 septembre 2026",
    "intervenant": "Eric Badarou",
}

PROJECT_ID = "training-project-483615"
SERVICE_ACCOUNT_KEY = "dbt_training_service_account_key.json"
STUDENTS_FILE = "students.json"
ADMIN_PASSWORD = "aivancity2026"

# Rôles accordés AU NIVEAU DU PROJET. Volontairement réduits au strict nécessaire :
# `jobUser` permet de lancer des requêtes (il n'existe pas d'équivalent par dataset),
# `aiplatform.user` permet d'appeler AI.CLASSIFY / AI.SCORE.
# La LECTURE des données n'est PAS accordée ici : elle est donnée dataset par dataset
# ci-dessous, sinon les étudiants verraient tous les datasets du projet.
ROLES = {
    "bigquery.jobUser": "roles/bigquery.jobUser",
    "aiplatform.user": "roles/aiplatform.user",
}

# Datasets que les étudiants peuvent LIRE. Accès accordé au niveau du dataset.
DATASETS_AUTORISES = ["social_data_course", "social_data_exam"]

# Rôles projet à retirer s'ils traînent d'une promotion précédente : ils donnent
# accès en lecture à TOUT le projet et annulent la restriction par dataset.
ROLES_TROP_LARGES = {
    "bigquery.user": "roles/bigquery.user",
    "bigquery.dataViewer": "roles/bigquery.dataViewer",
    "bigquery.dataEditor": "roles/bigquery.dataEditor",
}

# Mapping numero -> perimetre de l'examen final.
# Fige par la graine du generateur (RANDOM_SEED = 20260621) ; source de verite :
# "3. Examen final/INTERNE - ne pas diffuser/Examen_Final_Tirage_Correspondance.csv".
#
# La cellule 35 (Brisa x TikTok) est VOLONTAIREMENT ABSENTE : c'est le perimetre
# des 3 TP, de la requete canari du J4-C3 et du template Looker. L'etudiant qui la
# tirerait aurait passe quatre jours sur exactement ces donnees.
# -> 39 perimetres reellement attribuables.
PERIMETRES = [
    (1, "UrbanPulse", "Instagram", "urbanpulse", "instagram"),
    (2, "FitForm", "YouTube", "fitform", "youtube"),
    (3, "Maison Lumiere", "Pinterest", "maison_lumiere", "pinterest"),
    (4, "Maison Lumiere", "TikTok", "maison_lumiere", "tiktok"),
    (5, "Vestio", "Instagram", "vestio", "instagram"),
    (6, "UrbanPulse", "TikTok", "urbanpulse", "tiktok"),
    (7, "UrbanPulse", "YouTube", "urbanpulse", "youtube"),
    (8, "Vestio", "TikTok", "vestio", "tiktok"),
    (9, "Brisa", "X (Twitter)", "brisa", "x"),
    (10, "Maison Lumiere", "YouTube", "maison_lumiere", "youtube"),
    (11, "Celeste", "Instagram", "celeste", "instagram"),
    (12, "Celeste", "YouTube", "celeste", "youtube"),
    (13, "Atelier Roux", "Instagram", "atelier_roux", "instagram"),
    (14, "Celeste", "Pinterest", "celeste", "pinterest"),
    (15, "FitForm", "TikTok", "fitform", "tiktok"),
    (16, "Atelier Roux", "X (Twitter)", "atelier_roux", "x"),
    (17, "Vestio", "Pinterest", "vestio", "pinterest"),
    (18, "Brisa", "Pinterest", "brisa", "pinterest"),
    (19, "Vestio", "X (Twitter)", "vestio", "x"),
    (20, "Atelier Roux", "Pinterest", "atelier_roux", "pinterest"),
    (21, "FitForm", "X (Twitter)", "fitform", "x"),
    (22, "FitForm", "Instagram", "fitform", "instagram"),
    (23, "Maison Lumiere", "Instagram", "maison_lumiere", "instagram"),
    (24, "Nordska", "TikTok", "nordska", "tiktok"),
    (25, "UrbanPulse", "X (Twitter)", "urbanpulse", "x"),
    (26, "Atelier Roux", "TikTok", "atelier_roux", "tiktok"),
    (27, "Celeste", "X (Twitter)", "celeste", "x"),
    (28, "Celeste", "TikTok", "celeste", "tiktok"),
    (29, "Brisa", "YouTube", "brisa", "youtube"),
    (30, "Nordska", "Pinterest", "nordska", "pinterest"),
    (31, "FitForm", "Pinterest", "fitform", "pinterest"),
    (32, "UrbanPulse", "Pinterest", "urbanpulse", "pinterest"),
    (33, "Brisa", "Instagram", "brisa", "instagram"),
    (34, "Vestio", "YouTube", "vestio", "youtube"),
    # (35, "Brisa", "TikTok", ...) -> RETIRE : perimetre d'entrainement des TP.
    (36, "Nordska", "Instagram", "nordska", "instagram"),
    (37, "Nordska", "YouTube", "nordska", "youtube"),
    (38, "Nordska", "X (Twitter)", "nordska", "x"),
    (39, "Maison Lumiere", "X (Twitter)", "maison_lumiere", "x"),
    (40, "Atelier Roux", "YouTube", "atelier_roux", "youtube"),
]

# --- Helpers ---

def load_students():
    if os.path.exists(STUDENTS_FILE):
        with open(STUDENTS_FILE, "r") as f:
            return json.load(f)
    return []


def save_students(students):
    with open(STUDENTS_FILE, "w") as f:
        json.dump(students, f, indent=2)


def get_credentials():
    # Déployé sur Streamlit Cloud → secrets
    try:
        has_secrets = "gcp_service_account" in st.secrets
    except Exception:
        has_secrets = False
    if has_secrets:
        sa_info = dict(st.secrets["gcp_service_account"])
        return service_account.Credentials.from_service_account_info(sa_info)
    # Local → fichier JSON
    if os.path.exists(SERVICE_ACCOUNT_KEY):
        return service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_KEY)
    st.error("Aucune credentials trouvée (ni secrets Streamlit, ni fichier JSON local).")
    st.stop()


def get_iam_policy(client, project_id):
    request = iam_policy_pb2.GetIamPolicyRequest(
        resource=f"projects/{project_id}"
    )
    return client.get_iam_policy(request=request)


def set_iam_policy(client, project_id, policy):
    request = iam_policy_pb2.SetIamPolicyRequest(
        resource=f"projects/{project_id}",
        policy=policy,
    )
    return client.set_iam_policy(request=request)


def add_roles(client, project_id, email, roles):
    policy = get_iam_policy(client, project_id)
    member = f"user:{email}"

    for role in roles:
        binding_found = False
        for binding in policy.bindings:
            if binding.role == role:
                if member not in binding.members:
                    binding.members.append(member)
                binding_found = True
                break
        if not binding_found:
            new_binding = policy_pb2.Binding(role=role, members=[member])
            policy.bindings.append(new_binding)

    set_iam_policy(client, project_id, policy)


def remove_roles(client, project_id, email, roles):
    policy = get_iam_policy(client, project_id)
    member = f"user:{email}"

    for binding in policy.bindings:
        if binding.role in roles and member in binding.members:
            binding.members.remove(member)

    set_iam_policy(client, project_id, policy)


def bq_client(credentials, project_id):
    return bigquery.Client(project=project_id, credentials=credentials, location="EU")


def dataset_access(bqc, project_id, dataset_id, emails, accorder=True):
    """Ajoute ou retire des lecteurs sur UN dataset, sans toucher aux autres accès.

    Renvoie la liste des emails refusés par l'API. BigQuery rejette toute la requête
    si UNE adresse ne correspond pas à un compte Google existant ; on réessaie donc
    email par email pour isoler les fautives au lieu de tout perdre.
    """
    def ecrire(cibles):
        ds = bqc.get_dataset(f"{project_id}.{dataset_id}")
        entries = list(ds.access_entries)
        if accorder:
            presents = {e.entity_id for e in entries
                        if e.entity_type == "userByEmail" and e.role == "READER"}
            for email in cibles:
                if email not in presents:
                    entries.append(bigquery.AccessEntry("READER", "userByEmail", email))
        else:
            enleve = set(cibles)
            entries = [e for e in entries
                       if not (e.entity_type == "userByEmail" and e.role == "READER"
                               and e.entity_id in enleve)]
        ds.access_entries = entries
        bqc.update_dataset(ds, ["access_entries"])

    emails = list(emails)
    try:
        ecrire(emails)
        return []
    except Exception:
        refuses = []
        for email in emails:
            try:
                ecrire([email])
            except Exception:
                refuses.append(email)
        return refuses


def dataset_readers(bqc, project_id, dataset_id):
    """Emails ayant un accès lecture sur ce dataset."""
    ds = bqc.get_dataset(f"{project_id}.{dataset_id}")
    return {e.entity_id for e in ds.access_entries
            if e.entity_type == "userByEmail" and e.role == "READER"}


def get_roles_by_student(client, project_id, students):
    """Lit la policy IAM du projet et renvoie {email: [libelles de roles reellement accordes]}.

    On interroge GCP plutot que de se fier a un etat local : c'est la seule facon
    de refleter les roles reels, y compris ceux accordes hors de cette app.
    """
    policy = get_iam_policy(client, project_id)
    label_by_role = {v: k for k, v in ROLES.items()}

    granted = {email: [] for email in students}
    for binding in policy.bindings:
        label = label_by_role.get(binding.role)
        if label is None:
            continue
        for email in students:
            if f"user:{email}" in binding.members:
                granted[email].append(label)

    # Ordre stable : celui du dict ROLES, pas celui de la policy
    order = list(ROLES.keys())
    for email in granted:
        granted[email].sort(key=order.index)
    return granted


def build_exam_assignments_csv(students):
    """Liste nominative pour le surveillant : un etudiant = un perimetre d'examen.

    Attribution dans l'ORDRE D'INSCRIPTION (ordre de students.json), surtout pas
    par tri alphabetique : un nouvel inscrit doit recevoir le numero libre suivant
    sans jamais deplacer ceux deja attribues. La liste se remplit au fil des jours,
    donc un export du mardi doit rester valable le jeudi.

    Le brassage vient du mapping numero -> cellule, randomise a la generation des
    donnees : le 1er inscrit ne tombe pas sur la 1re marque de la liste.

    PLUS D'ETUDIANTS QUE DE PERIMETRES (53 pour 39) : on RECOMMENCE la liste au
    lieu de tronquer. Surtout pas un zip(), qui ferait disparaitre silencieusement
    les etudiants au-dela du 39e du fichier remis au surveillant.
    La colonne "partage_avec" signale les periemtres attribues deux fois, pour que
    le surveillant n'assoie pas les deux etudiants cote a cote.
    """
    total = len(students)
    nb_perimetres = len(PERIMETRES)

    # Combien de fois chaque perimetre est-il utilise pour cet effectif ?
    occurrences = {}
    for i in range(total):
        numero = PERIMETRES[i % nb_perimetres][0]
        occurrences.setdefault(numero, []).append(students[i])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["numero", "email", "nom_a_completer", "marque", "plateforme",
                     "brand_id", "platform_id", "partage_avec"])

    for i, email in enumerate(students):
        numero, marque, plateforme, brand_id, platform_id = PERIMETRES[i % nb_perimetres]
        autres = [e for e in occurrences[numero] if e != email]
        partage = " ; ".join(autres) if autres else ""
        writer.writerow([numero, email, "", marque, plateforme,
                         brand_id, platform_id, partage])

    return buffer.getvalue()


def build_students_csv(granted):
    """CSV : une ligne par etudiant, une colonne Oui/Non par role, + date d'export."""
    today = date.today().isoformat()
    labels = list(ROLES.keys())

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["email"] + labels + ["nb_roles", "roles_accordes", "date_export"])

    for email in sorted(granted):
        roles = granted[email]
        writer.writerow(
            [email]
            + ["Oui" if label in roles else "Non" for label in labels]
            + [len(roles), ";".join(roles), today]
        )

    return buffer.getvalue()


# --- App ---

st.set_page_config(page_title=f"Accès — {COURS['nom']}", page_icon="🔐", layout="wide")
st.title(f"Accès BigQuery — {COURS['nom']}")
st.caption(
    f"{COURS['programme']} · {COURS['dates']} · intervenant : {COURS['intervenant']}  \n"
    f"Projet `{PROJECT_ID}` · datasets accessibles : "
    + ", ".join(f"`{d}`" for d in DATASETS_AUTORISES)
)

credentials = get_credentials()
client = resourcemanager_v3.ProjectsClient(credentials=credentials)
bqc = bq_client(credentials, PROJECT_ID)
students = load_students()

# --- Tabs ---
tab_register, tab_admin = st.tabs(["📝 Inscription étudiant", "🔧 Admin"])

# --- Tab 1 : Inscription étudiant ---
with tab_register:
    st.header(f"Inscription — {COURS['nom']}")
    st.write(
        f"Séminaire **{COURS['nom']}** ({COURS['programme']}), {COURS['dates']}.\n\n"
        "Renseignez l'adresse **de votre compte Google** pour accéder aux données BigQuery du cours. "
        "C'est ce même compte que vous devrez utiliser en TP et à l'examen."
    )

    with st.form("register_form"):
        email = st.text_input("Adresse email Google")
        submitted = st.form_submit_button("S'inscrire")

        if submitted and email:
            email = email.strip().lower()
            if "@" not in email or "." not in email.split("@")[-1]:
                st.warning("Vérifiez que c'est bien une adresse email valide.")
            elif email in students:
                st.info("Vous êtes déjà inscrit(e).")
            else:
                students.append(email)
                save_students(students)
                st.success(f"✅ {email} inscrit(e) avec succès !")

# --- Tab 2 : Admin ---
with tab_admin:
    st.header("Gestion des accès")

    password = st.text_input("Mot de passe admin", type="password")
    if password != ADMIN_PASSWORD:
        if password:
            st.error("Mot de passe incorrect.")
        st.stop()

    if not students:
        st.info("Aucun étudiant inscrit pour le moment.")
    else:
        st.write(f"**{len(students)} étudiant(s) inscrit(s)**")

        # Les rôles sont relus à chaque exécution du script ; ce bouton force simplement
        # un rerun pour rafraîchir après un changement fait ailleurs (console GCP).
        st.button("🔄 Rafraîchir les rôles depuis GCP")

        try:
            granted = get_roles_by_student(client, PROJECT_ID, students)
        except Exception as e:
            st.error(f"Impossible de lire la policy IAM : {e}")
            granted = {email: [] for email in students}

        try:
            lecteurs = {ds: dataset_readers(bqc, PROJECT_ID, ds) for ds in DATASETS_AUTORISES}
        except Exception as e:
            st.warning(f"Accès dataset illisibles : {e}")
            lecteurs = {ds: set() for ds in DATASETS_AUTORISES}

        st.dataframe(
            {
                "Email": sorted(granted),
                "Rôles projet": [", ".join(granted[e]) or "— aucun —" for e in sorted(granted)],
                "Datasets lisibles": [
                    ", ".join(ds for ds in DATASETS_AUTORISES if e in lecteurs[ds]) or "— aucun —"
                    for e in sorted(granted)
                ],
            },
            width="stretch",
        )

        sans_data = [e for e in granted
                     if not any(e in lecteurs[ds] for ds in DATASETS_AUTORISES)]
        if sans_data:
            st.info(
                f"{len(sans_data)} étudiant(s) sans accès aux données. "
                "Les rôles projet seuls ne suffisent pas : il faut aussi l'accès dataset."
            )

        sans_roles = [e for e in granted if not granted[e]]
        if sans_roles:
            st.warning(f"{len(sans_roles)} étudiant(s) inscrit(s) sans aucun rôle : {', '.join(sans_roles)}")

        sans_ai = [e for e in granted if "aiplatform.user" not in granted[e]]
        if sans_ai:
            st.info(
                f"{len(sans_ai)} étudiant(s) sans `aiplatform.user` — "
                "ils ne pourront pas lancer AI.CLASSIFY / AI.SCORE au Jour 2."
            )

        col_dl1, col_dl2 = st.columns(2)

        with col_dl1:
            st.download_button(
                "📥 Liste + rôles (CSV)",
                data=build_students_csv(granted),
                file_name=f"roles_{COURS['nom'].replace(' ','_').replace('&','et')}_{date.today().isoformat()}.csv",
                mime="text/csv",
                width="stretch",
            )

        with col_dl2:
            st.download_button(
                "🎓 Attributions d'examen (CSV)",
                data=build_exam_assignments_csv(students),
                file_name=f"attributions_examen_{COURS['nom'].replace(' ','_').replace('&','et')}_{date.today().isoformat()}.csv",
                mime="text/csv",
                width="stretch",
                help="Liste nominative à remettre au surveillant : un étudiant, un périmètre.",
            )
            if len(students) > len(PERIMETRES):
                partages = len(students) - len(PERIMETRES)
                st.warning(
                    f"{len(students)} étudiants pour {len(PERIMETRES)} périmètres : "
                    f"**{partages} périmètres seront attribués à deux étudiants** "
                    f"({len(PERIMETRES) - partages} uniques). C'est assumé — l'épreuve est "
                    "individuelle et surveillée. La colonne `partage_avec` du CSV indique "
                    "les binômes, à ne pas placer côte à côte."
                )

        st.divider()

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Attribuer les accès")
            roles_to_add = st.multiselect(
                "Rôles à attribuer",
                options=list(ROLES.keys()),
                default=list(ROLES.keys()),
                key="add_roles",
            )
            st.caption(
                "Les rôles projet ci-dessus ne donnent **aucun** accès aux données. "
                f"La lecture est accordée dataset par dataset sur : {', '.join(DATASETS_AUTORISES)}."
            )
            if st.button("✅ Attribuer à tous les étudiants", type="primary"):
                role_values = [ROLES[r] for r in roles_to_add]
                progress = st.progress(0)
                failed = []
                for i, student_email in enumerate(students):
                    try:
                        add_roles(client, PROJECT_ID, student_email, role_values)
                        # Retirer les rôles projet trop larges hérités d'une promo précédente
                        remove_roles(client, PROJECT_ID, student_email,
                                     list(ROLES_TROP_LARGES.values()))
                    except Exception as e:
                        failed.append((student_email, str(e)))
                    progress.progress((i + 1) / len(students))
                # Accès lecture, dataset par dataset, en une seule écriture par dataset
                for ds_id in DATASETS_AUTORISES:
                    try:
                        refuses = dataset_access(bqc, PROJECT_ID, ds_id, students, accorder=True)
                        if refuses:
                            st.error(
                                f"`{ds_id}` : {len(refuses)} adresse(s) refusée(s) — "
                                "ce ne sont pas des comptes Google existants. "
                                f"À corriger : {', '.join(refuses)}"
                            )
                        else:
                            st.caption(f"Lecture accordée sur `{ds_id}`")
                    except Exception as e:
                        failed.append((ds_id, str(e)))
                if failed:
                    st.warning(f"{len(failed)} erreur(s) :")
                    for email_err, err_msg in failed:
                        st.error(f"**{email_err}** : {err_msg}")
                succeeded = len(students) - len(failed)
                if succeeded > 0:
                    st.success(f"Accès attribués à {succeeded} étudiant(s).")

        with col2:
            st.subheader("Retirer les accès")
            roles_to_remove = st.multiselect(
                "Rôles à retirer",
                options=list(ROLES.keys()),
                default=list(ROLES.keys()),
                key="remove_roles",
            )
            if st.button("🚫 Retirer à tous les étudiants", type="secondary"):
                role_values = [ROLES[r] for r in roles_to_remove]
                progress = st.progress(0)
                failed = []
                for i, student_email in enumerate(students):
                    try:
                        remove_roles(client, PROJECT_ID, student_email, role_values)
                    except Exception as e:
                        failed.append((student_email, str(e)))
                    progress.progress((i + 1) / len(students))
                for ds_id in DATASETS_AUTORISES:
                    try:
                        dataset_access(bqc, PROJECT_ID, ds_id, students, accorder=False)
                    except Exception as e:
                        failed.append((ds_id, str(e)))
                if failed:
                    st.warning(f"{len(failed)} erreur(s) :")
                    for email_err, err_msg in failed:
                        st.error(f"**{email_err}** : {err_msg}")
                succeeded = len(students) - len(failed)
                if succeeded > 0:
                    st.success(f"Accès retirés pour {succeeded} étudiant(s).")

        st.divider()

        st.subheader("Gérer individuellement")
        selected_email = st.selectbox("Sélectionner un étudiant", students)

        col3, col4 = st.columns(2)
        with col3:
            if st.button(f"✅ Attribuer les accès à {selected_email}"):
                add_roles(client, PROJECT_ID, selected_email, list(ROLES.values()))
                remove_roles(client, PROJECT_ID, selected_email, list(ROLES_TROP_LARGES.values()))
                for ds_id in DATASETS_AUTORISES:
                    dataset_access(bqc, PROJECT_ID, ds_id, [selected_email], accorder=True)
                st.success(f"Accès attribués à {selected_email} ({', '.join(DATASETS_AUTORISES)})")
        with col4:
            if st.button(f"🚫 Retirer les accès de {selected_email}"):
                remove_roles(client, PROJECT_ID, selected_email, list(ROLES.values()))
                for ds_id in DATASETS_AUTORISES:
                    dataset_access(bqc, PROJECT_ID, ds_id, [selected_email], accorder=False)
                st.success(f"Accès retirés pour {selected_email}")

        st.divider()

        st.subheader("Supprimer un étudiant")
        email_to_remove = st.selectbox("Étudiant à supprimer", students, key="remove_student")
        if st.button("🗑️ Supprimer de la liste", type="secondary"):
            remove_roles(client, PROJECT_ID, email_to_remove, list(ROLES.values()))
            for ds_id in DATASETS_AUTORISES:
                dataset_access(bqc, PROJECT_ID, ds_id, [email_to_remove], accorder=False)
            students.remove(email_to_remove)
            save_students(students)
            st.success(f"{email_to_remove} supprimé(e) et accès retirés.")
            st.rerun()
