# trader-agents infrastructure

Pulumi (TypeScript) stack that provisions private **ECR** repositories for the API and web images, plus an **IAM user** with access keys for push/pull.

## Prerequisites

- [Pulumi CLI](https://www.pulumi.com/docs/install/)
- Node.js 20+
- AWS credentials with permission to manage ECR and IAM (for `pulumi up`)

## Bootstrap

```bash
cd infra
npm install
pulumi stack select dev   # or: pulumi stack init dev
pulumi up
```

Optional config (defaults shown):

```bash
pulumi config set aws:region ap-southeast-1
pulumi config set ecrUserName trader-ecr
```

## Outputs

After `pulumi up`, capture:

```bash
pulumi stack output apiRepositoryUrl
pulumi stack output webRepositoryUrl
pulumi stack output awsRegion
pulumi stack output accessKeyId
pulumi stack output --show-secrets secretAccessKey
```

Use `accessKeyId` / `secretAccessKey` on the Linux box (and your build machine) in an AWS CLI profile, e.g. `trader-ecr`. Do not commit these values.

## Destroy

```bash
pulumi destroy
```

Repos may refuse destroy if images remain; this stack sets `forceDelete: true` on the repositories so empty/force cleanup works with Pulumi.
