#!/usr/bin/env bash
#
# enable_apis.sh — enable the GCP APIs NERVE needs to build and deploy.
#
# Run ONCE per GCP project, before secrets_setup.sh and the first Cloud Build.
# Authenticate and select the project first:
#     gcloud auth login
#     gcloud config set project YOUR_PROJECT_ID
#
# Then:
#     ./deploy/enable_apis.sh
#
# Enabling an already-enabled API is a no-op, so this is safe to re-run.

set -euo pipefail

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com

echo "Enabled: run, cloudbuild, artifactregistry, secretmanager"
