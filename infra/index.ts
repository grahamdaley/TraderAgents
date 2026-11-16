import * as aws from "@pulumi/aws";
import * as pulumi from "@pulumi/pulumi";

const config = new pulumi.Config();
const ecrUserName = config.get("ecrUserName") ?? "trader-ecr";

const lifecyclePolicyDocument = JSON.stringify({
  rules: [
    {
      rulePriority: 1,
      description: "Keep last 5 images",
      selection: {
        tagStatus: "any",
        countType: "imageCountMoreThan",
        countNumber: 5,
      },
      action: { type: "expire" },
    },
  ],
});

function createRepository(name: string): aws.ecr.Repository {
  const repo = new aws.ecr.Repository(name, {
    name,
    imageScanningConfiguration: { scanOnPush: true },
    imageTagMutability: "MUTABLE",
    forceDelete: true,
  });

  new aws.ecr.LifecyclePolicy(`${name}-lifecycle`, {
    repository: repo.name,
    policy: lifecyclePolicyDocument,
  });

  return repo;
}

const apiRepo = createRepository("trader-api");
const webRepo = createRepository("trader-web");

const ecrUser = new aws.iam.User("trader-ecr-user", {
  name: ecrUserName,
  path: "/trader-agents/",
});

const ecrPolicy = new aws.iam.UserPolicy("trader-ecr-policy", {
  user: ecrUser.name,
  policy: pulumi
    .all([apiRepo.arn, webRepo.arn])
    .apply(([apiArn, webArn]) =>
      JSON.stringify({
        Version: "2012-10-17",
        Statement: [
          {
            Sid: "EcrAuthToken",
            Effect: "Allow",
            Action: ["ecr:GetAuthorizationToken"],
            Resource: "*",
          },
          {
            Sid: "EcrPushPull",
            Effect: "Allow",
            Action: [
              "ecr:BatchCheckLayerAvailability",
              "ecr:GetDownloadUrlForLayer",
              "ecr:BatchGetImage",
              "ecr:DescribeRepositories",
              "ecr:ListImages",
              "ecr:DescribeImages",
              "ecr:InitiateLayerUpload",
              "ecr:UploadLayerPart",
              "ecr:CompleteLayerUpload",
              "ecr:PutImage",
            ],
            Resource: [apiArn, webArn],
          },
        ],
      }),
    ),
});

const accessKey = new aws.iam.AccessKey("trader-ecr-access-key", {
  user: ecrUser.name,
}, { dependsOn: [ecrPolicy] });

const region = aws.config.region ?? "ap-southeast-1";

export const apiRepositoryUrl = apiRepo.repositoryUrl;
export const webRepositoryUrl = webRepo.repositoryUrl;
export const apiRepositoryName = apiRepo.name;
export const webRepositoryName = webRepo.name;
export const registryId = apiRepo.registryId;
export const awsRegion = region;
export const ecrUserArn = ecrUser.arn;
export const accessKeyId = accessKey.id;
export const secretAccessKey = pulumi.secret(accessKey.secret);
