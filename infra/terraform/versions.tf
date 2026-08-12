terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state is strongly recommended. Configure with a backend file so no
  # account-specific value is committed:
  #   terraform init -backend-config=backend.hcl
  #
  # backend "s3" {
  #   bucket       = "..."
  #   key          = "techsara-chat-archive/terraform.tfstate"
  #   region       = "..."
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Component   = "chatgpt-session-archive"
      DataClass   = "confidential"
    }
  }
}
