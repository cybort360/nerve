#!/usr/bin/env bash
#
# secrets_setup.sh — create the four NERVE secrets in Google Cloud Secret Manager.
#
# Run ONCE per GCP project. First authenticate and select the project:
#     gcloud auth login
#     gcloud config set project YOUR_PROJECT_ID
#     gcloud services enable secretmanager.googleapis.com
#
# Replace each REPLACE_WITH_* placeholder below with the real value before running
# (or export it as an env var and substitute). Re-running for a secret that already
# exists will fail — to rotate a value, use:
#     echo "NEW_VALUE" | gcloud secrets versions add NAME --data-file=-
#
# After creating the secrets, grant the Cloud Run service account read access:
#     gcloud secrets add-iam-policy-binding NAME \
#       --member="serviceAccount:SERVICE_ACCOUNT_EMAIL" \
#       --role="roles/secretmanager.secretAccessor"

set -euo pipefail

# MongoDB Atlas connection string, e.g. mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&w=majority
echo "REPLACE_WITH_MONGODB_URI" | gcloud secrets create nerve-mongodb-uri --data-file=-

# Dynatrace API token with scopes: Read problems, Read metrics, Read entities.
echo "REPLACE_WITH_DYNATRACE_API_TOKEN" | gcloud secrets create nerve-dynatrace-api-token --data-file=-

# GitLab personal access token with the `api` scope.
echo "REPLACE_WITH_GITLAB_TOKEN" | gcloud secrets create nerve-gitlab-token --data-file=-

# Telegram bot token from @BotFather (format 123456789:ABC...). Powers the mobile
# approval/notification bot; required only when TELEGRAM_ENABLED=true at runtime.
echo "REPLACE_WITH_TELEGRAM_BOT_TOKEN" | gcloud secrets create nerve-telegram-bot-token --data-file=-

echo "Created secrets: nerve-mongodb-uri, nerve-dynatrace-api-token, nerve-gitlab-token, nerve-telegram-bot-token"
