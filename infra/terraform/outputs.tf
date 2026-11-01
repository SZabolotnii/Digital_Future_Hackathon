# Значення для .github/workflows/ci.yml і cloudbuild.yaml.
output "wif_provider" {
  value = module.cicd.wif_provider
}

output "gha_service_account" {
  value = module.iam.gha_email
}

output "image" {
  value = module.registry.image
}

output "demo_url" {
  value = module.run.demo_url
}
