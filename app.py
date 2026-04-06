import streamlit as st
import json
import os
from google.cloud import resourcemanager_v3
from google.iam.v1 import iam_policy_pb2, policy_pb2
from google.oauth2 import service_account

# --- Config ---
PROJECT_ID = "training-project-483615"
SERVICE_ACCOUNT_KEY = "dbt_training_service_account_key.json"
STUDENTS_FILE = "students.json"
ADMIN_PASSWORD = "aivancity2026"

ROLES = {
    "bigquery.user": "roles/bigquery.user",
    "bigquery.dataViewer": "roles/bigquery.dataViewer",
    "bigquery.dataEditor": "roles/bigquery.dataEditor",
    "bigquery.jobUser": "roles/bigquery.jobUser",
}

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


# --- App ---

st.set_page_config(page_title="GCP Access Manager", page_icon="🔐", layout="wide")
st.title("GCP Access Manager")
st.caption(f"Projet : `{PROJECT_ID}`")

credentials = get_credentials()
client = resourcemanager_v3.ProjectsClient(credentials=credentials)
students = load_students()

# --- Tabs ---
tab_register, tab_admin = st.tabs(["📝 Inscription étudiant", "🔧 Admin"])

# --- Tab 1 : Inscription étudiant ---
with tab_register:
    st.header("Inscription")
    st.write("Renseignez votre adresse email Google pour accéder aux ressources BigQuery du cours.")

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
        st.dataframe({"Email": students}, width="stretch")
        st.download_button(
            "📥 Exporter la liste (CSV)",
            data="email\n" + "\n".join(students),
            file_name="students.csv",
            mime="text/csv",
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
            if st.button("✅ Attribuer à tous les étudiants", type="primary"):
                role_values = [ROLES[r] for r in roles_to_add]
                progress = st.progress(0)
                for i, student_email in enumerate(students):
                    add_roles(client, PROJECT_ID, student_email, role_values)
                    progress.progress((i + 1) / len(students))
                st.success(f"Accès attribués à {len(students)} étudiant(s).")

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
                for i, student_email in enumerate(students):
                    remove_roles(client, PROJECT_ID, student_email, role_values)
                    progress.progress((i + 1) / len(students))
                st.success(f"Accès retirés pour {len(students)} étudiant(s).")

        st.divider()

        st.subheader("Gérer individuellement")
        selected_email = st.selectbox("Sélectionner un étudiant", students)

        col3, col4 = st.columns(2)
        with col3:
            if st.button(f"✅ Attribuer les accès à {selected_email}"):
                add_roles(client, PROJECT_ID, selected_email, list(ROLES.values()))
                st.success(f"Accès attribués à {selected_email}")
        with col4:
            if st.button(f"🚫 Retirer les accès de {selected_email}"):
                remove_roles(client, PROJECT_ID, selected_email, list(ROLES.values()))
                st.success(f"Accès retirés pour {selected_email}")

        st.divider()

        st.subheader("Supprimer un étudiant")
        email_to_remove = st.selectbox("Étudiant à supprimer", students, key="remove_student")
        if st.button("🗑️ Supprimer de la liste", type="secondary"):
            remove_roles(client, PROJECT_ID, email_to_remove, list(ROLES.values()))
            students.remove(email_to_remove)
            save_students(students)
            st.success(f"{email_to_remove} supprimé(e) et accès retirés.")
            st.rerun()
