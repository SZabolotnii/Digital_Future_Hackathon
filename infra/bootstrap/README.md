# Bootstrap: кроки поза Terraform

Усе, що лишається в GCP, описано в `infra/terraform/` (Spec INV-8). Нижче — єдині кроки, які робляться руками (FR-IAC-008), кожен однією командою.

## 1. Bucket для стану Terraform

Terraform зберігає стан у цьому bucket, тож створити його сам не може.

```bash
P=apps-deploy-512911
gcloud storage buckets create gs://$P-tfstate --project $P --location europe-central2 \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update gs://$P-tfstate --versioning
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"isLive":false,"numNewerVersions":10}}]}' > lc.json
gcloud storage buckets update gs://$P-tfstate --lifecycle-file=lc.json
```

## 2. API, без яких Terraform не стартує

`cloudresourcemanager` і `cloudbilling` потрібні провайдеру ще до того, як Terraform почне керувати API. Далі вони описані в `google_project_service`.

```bash
gcloud services enable cloudresourcemanager.googleapis.com cloudbilling.googleapis.com --project $P
```

## 3. Облікові дані власника

Spec передбачає `gcloud auth application-default login`. Якщо ADC на машині вже зайняті іншим проєктом, Terraform отримує короткий токен з окремої конфігурації gcloud і глобальних облікових даних не торкається:

```bash
gcloud config configurations create livemedcard   # один раз
gcloud auth login <owner-account> --configuration=livemedcard
export GOOGLE_OAUTH_ACCESS_TOKEN=$(CLOUDSDK_ACTIVE_CONFIG_NAME=livemedcard gcloud auth print-access-token)
cd infra/terraform && terraform init && terraform plan
```

Токен живе годину; після цього команду `export` повторюють.

## 4. Підключення GitHub до Cloud Build

OAuth і встановлення GitHub App робляться лише в браузері. Спершу `terraform apply` видає сервісному агенту Cloud Build роль `secretmanager.admin` (модуль `cicd`), бо підключення зберігає токен GitHub у Secret Manager. Потім:

```bash
gcloud builds connections create github lmc-github --region=europe-central2 --project=$P
```

Команда друкує посилання. Відкрити його під власником проєкту, дозволити Google Cloud Build доступ до GitHub-акаунта й установити застосунок лише на репозиторій `Digital_Future_Hackathon`. Перевірка: `gcloud builds connections describe lmc-github --region=europe-central2` показує `stage: COMPLETE`. Репозиторій у підключенні і trigger `lmc-deploy` далі створює Terraform.

## 5. Пізніше

- DNS-запис для GKE Ingress, тиждень 8.
