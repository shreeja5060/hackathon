# Keeping reviews on Google Cloud

The dashboard writes every upload, analysis and review decision to the **ledger**
(`shared/ledger.py`, two SQLite files under `data/`), and saves uploaded PDFs and
approved starter policies as files named by their SHA-256. Locally that is all on
your disk. On Cloud Run the container's disk disappears whenever an instance stops,
so two things are added:

| What | Where it lives on Cloud Run | How |
| --- | --- | --- |
| Ledger (decisions, trail) | the container's disk, streamed to Cloud Storage | [Litestream](https://litestream.io), started by `deploy/start.sh` |
| Original documents | a Cloud Storage bucket mounted as a folder | Cloud Run volume mount |

When a new instance starts, `start.sh` restores the ledger from the bucket before the
dashboard opens. Changes reach the bucket about a second after they happen.
Tested locally: a fresh instance with an empty disk restored every entry, and the
hash chain still verified.

## Commands

Run these in Cloud Shell (already signed in to the project). The service name and
region are the ones the team uses today; change them if yours differ.

```bash
PROJECT=uc2-cyber-policy-compliance
REGION=us-central1
SERVICE=hackathon-git
BUCKET=uc2-copilot-records          # must be unique across Cloud Storage
gcloud config set project $PROJECT
PROJECT_NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')

# 1. A private bucket for the ledger replica and the documents. Versioning keeps
#    overwritten or deleted objects recoverable.
gcloud storage buckets create gs://$BUCKET --location=$REGION \
    --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://$BUCKET --versioning

# 2. Let the service's identity read and write that bucket (only that bucket).
SA=$(gcloud run services describe $SERVICE --region=$REGION \
    --format='value(spec.template.spec.serviceAccountName)')
SA=${SA:-$PROJECT_NUMBER-compute@developer.gserviceaccount.com}
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
    --member=serviceAccount:$SA --role=roles/storage.objectAdmin

# 3. The Anthropic key from Secret Manager, never in the image or the repo.
printf '%s' 'PASTE_THE_KEY' | gcloud secrets create anthropic-api-key --data-file=-
gcloud secrets add-iam-policy-binding anthropic-api-key \
    --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor

# 4. Memory, one instance (one writer for the ledger), CPU between requests
#    (so Litestream keeps streaming), the bucket mounted for documents (owned by the
#    image's user, uid 1000, so the app can write to it).
gcloud run services update $SERVICE --region=$REGION \
    --memory=4Gi --cpu=2 \
    --min-instances=1 --max-instances=1 \
    --no-cpu-throttling --session-affinity \
    --execution-environment=gen2 \
    --add-volume="name=records,type=cloud-storage,bucket=$BUCKET,mount-options=uid=1000;gid=1000" \
    --add-volume-mount=volume=records,mount-path=/mnt/records \
    --update-env-vars=LEDGER_REPLICA_URL=gs://$BUCKET/ledger,COPILOT_DOCUMENT_DIR=/mnt/records/documents \
    --update-secrets=ANTHROPIC_API_KEY=anthropic-api-key:latest
```

Check it: open the service, analyze a sample policy, then restart the service
(`gcloud run services update $SERVICE --region=$REGION --update-env-vars=RESTART=$(date +%s)`).
The review is still in the Library, and its Activity trail says "Trail intact".

## Company sign-in (recommended)

With Identity-Aware Proxy, only people in the organization can open the dashboard,
and the reviewer's name comes from their signed-in account instead of a text box.
Under the two-person rule that matters: nobody can type a colleague's name to confirm
their own decision.

```bash
gcloud services identity create --service=iap.googleapis.com --project=$PROJECT
gcloud run services update $SERVICE --region=$REGION --iap
gcloud run services add-iam-policy-binding $SERVICE --region=$REGION \
    --member=serviceAccount:service-$PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com \
    --role=roles/run.invoker
gcloud run services remove-iam-policy-binding $SERVICE --region=$REGION \
    --member=allUsers --role=roles/run.invoker     # stop public access, if it was allowed
gcloud run services update $SERVICE --region=$REGION \
    --update-env-vars=COPILOT_IDENTITY=iap,COPILOT_IAP_AUDIENCE=/projects/$PROJECT_NUMBER/locations/$REGION/services/$SERVICE
```

Then, in the console: **Security > Identity-Aware Proxy**, select the service, **Add
principal**, give each reviewer the **IAP-secured Web App User** role. The dashboard
verifies IAP's signed token for that audience (`phase3_dashboard/core/identity.py`).

## Limits, and the production design

- **One instance.** SQLite and Litestream assume one writer, so `max-instances=1`. Plenty
  for a team's reviews; not for a whole company at once.
- **Deploys.** While a new revision starts, the old one may still run for a moment. Deploy
  when nobody is mid-review.
- **Production.** Move the ledger to **Cloud SQL for PostgreSQL** (managed backups and
  point-in-time recovery, many instances), keep documents in Cloud Storage with a
  **retention policy** so evidence can't be deleted early, and keep IAP and Secret
  Manager. `shared/ledger.py` is plain SQL with one connection per operation, so the
  change is a database driver and a few SQL details, not a redesign.
- **Elsewhere.** The image runs unchanged on OpenShift or any Kubernetes: mount a
  persistent volume at `/app/data` and leave `LEDGER_REPLICA_URL` unset.

## Files

- `deploy/start.sh`: the image's start command (restore, then run the app under Litestream).
- `deploy/litestream.yml`: which files are streamed where (live and simulator ledgers).
- `Dockerfile`: installs Litestream 0.5.17, checked against its published SHA-256.
