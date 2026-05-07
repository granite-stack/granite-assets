Infrastructure Setup
====================

Granite Assets is a pure-Python library — it does **not** manage infrastructure.
This page describes two reference architectures that work well with each backend,
and provides enough configuration detail to get a production-quality (or
development-quality) environment running.

.. contents:: On this page
   :local:
   :depth: 2

----

Local Setup: Nginx + tusd
--------------------------

This architecture is suitable for:

* local development environments,
* single-server production deployments (internal tools, small scale),
* environments where AWS costs or complexity are not justified.

How it works
~~~~~~~~~~~~

Two processes run side by side and share the same filesystem directory:

.. code-block:: text

   Client
     │
     ├─► POST /files/  ──► tusd  ──► writes to /srv/assets/{visibility}/{key}
     │                          (pre-create hook validates upload-token)
     │                          (post-finish hook moves file to correct path)
     │
     └─► GET /assets/…  ──► Nginx
               ├── /assets/public/  → served directly, no token
               └── /assets/private/ → requires secure_link token (md5+expires)

**Nginx** handles all reads.  **tusd** handles all writes via the
`tus resumable upload protocol <https://tus.io>`_.  Your application only needs
to generate signed URLs (download via ``build_download_url``, upload via
``build_upload_url``); it never touches binary data directly.

Docker Compose (manual / development)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The repository ships a ready-to-use compose file:

.. code-block:: bash

   # 1. Copy the env template and set both secrets
   cp .env.nginx-example .env.nginx
   $EDITOR .env.nginx

   # 2. Start both services
   docker compose -f docker-compose.nginx-manual.yml --env-file .env.nginx up

   # 3. Stop
   docker compose -f docker-compose.nginx-manual.yml down

``SECURE_LINK_SECRET`` is the Nginx ``secure_link_md5`` secret — Nginx uses it
to validate signed download URLs.  ``UPLOAD_SECRET`` is the HMAC-SHA256 secret
used by ``build_upload_url`` to sign upload tokens.  Both must match the values
in your ``LocalNginxAssetRepositoryConfig``.

.. code-block:: bash

   # Generate strong random secrets (recommended)
   python -c "import secrets; print(secrets.token_urlsafe(32))"

Services and ports
~~~~~~~~~~~~~~~~~~

+----------+------------------+------------------------------------------+
| Service  | Host port        | Purpose                                  |
+==========+==================+==========================================+
| ``nginx``| ``8080``         | Serve files (public + private signed)    |
+----------+------------------+------------------------------------------+
| ``tusd`` | ``1080``         | Accept resumable tus uploads             |
+----------+------------------+------------------------------------------+

.. tip::

   In production, both services typically sit behind a single TLS-terminating
   reverse proxy (Nginx itself, Caddy, HAProxy …) on ports 80/443.  Configure
   ``tusd`` to listen on an internal port and proxy ``/files/`` through the
   public-facing server.

Repository configuration
~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   import os
   from granite_assets import LocalNginxAssetRepositoryConfig, build_asset_repository

   config = LocalNginxAssetRepositoryConfig(
       storage_path="/srv/assets",               # shared with tusd
       base_url="http://localhost:8080/assets",  # how Nginx exposes files
       secure_link_secret=os.environ["SECURE_LINK_SECRET"],
       tusd_url="http://localhost:1080",
       upload_secret=os.environ["UPLOAD_SECRET"],
       upload_ttl_seconds=3600,
       secure_link_ttl_seconds=3600,
   )
   repo = build_asset_repository(config)

Upload flow
~~~~~~~~~~~

.. code-block:: python

   # 1. Your API endpoint calls build_upload_url
   result = repo.build_upload_url(
       "invoices/inv-001.pdf",
       "application/pdf",
       visibility=AssetVisibility.PRIVATE,
   )
   # result.url     → "http://localhost:1080/files/"
   # result.method  → "POST"
   # result.headers → {"Tus-Resumable": "1.0.0", "Upload-Metadata": "...", ...}

   # 2. Return the result to the client (e.g. as JSON from a FastAPI endpoint)

   # 3. The client performs the tus upload:
   #    POST {url}  with Upload-Length and the supplied headers  → 201 + Location
   #    PATCH {location} with the file chunks                   → 204 per chunk

.. code-block:: javascript

   // Browser / Node.js — using the tus-js-client library
   import { Upload } from 'tus-js-client';

   const upload = new Upload(file, {
       endpoint: result.url,
       headers: result.headers,
       metadata: {},   // tusd already received metadata in the creation POST
       onSuccess: () => fetch('/api/confirm', { method: 'POST',
           body: JSON.stringify({ key: result.key }) }),
   });
   upload.start();

Hook integration
~~~~~~~~~~~~~~~~

tusd calls HTTP hook endpoints at key lifecycle events so your application can
validate tokens and post-process completed uploads.

**pre-create** — validate the upload token before accepting the upload:

.. code-block:: python

   import hmac
   import base64
   from fastapi import Request, HTTPException

   UPLOAD_SECRET = os.environ["UPLOAD_SECRET"]

   @app.post("/tusd/hooks")
   async def tusd_hook(request: Request) -> dict:
       body = await request.json()
       hook_name = request.headers.get("Hook-Name", "")
       upload = body.get("Upload", {})
       meta = upload.get("MetaData", {})

       if hook_name == "pre-create":
           key          = meta.get("asset-key", "")
           visibility   = meta.get("visibility", "private")
           content_type = meta.get("content-type", "")
           expires      = int(meta.get("upload-expires", "0"))
           token        = meta.get("upload-token", "")

           import time
           if time.time() > expires:
               raise HTTPException(status_code=400, detail="Upload token expired")

           payload  = f"{expires}:{key}:{visibility}:{content_type}"
           expected = hmac.new(
               UPLOAD_SECRET.encode(), payload.encode(), "sha256"
           ).hexdigest()
           if not hmac.compare_digest(token, expected):
               raise HTTPException(status_code=403, detail="Invalid upload token")

       if hook_name == "post-finish":
           # Move the completed file to {storage_path}/{visibility}/{key}
           # and update your database record.
           ...

       return {}

**Configure hooks in docker-compose.nginx-manual.yml:**

.. code-block:: yaml

   tusd:
     command:
       - -upload-dir=/data
       - -port=8080
       - -hooks-http=http://your-app:8000/tusd/hooks

Nginx configuration
~~~~~~~~~~~~~~~~~~~

The ``examples/nginx-secure-link.conf`` file in the repository is an annotated
operator template.  It configures:

* ``/assets/public/`` — no token required.
* ``/assets/private/`` — validates ``?md5=TOKEN&expires=TIMESTAMP`` via
  ``ngx_http_secure_link_module``.

The secret placeholder (``${SECURE_LINK_SECRET}``) is expanded by ``envsubst``
at container startup so secrets never appear in version-controlled files.

----

AWS Setup: S3 + CloudFront
---------------------------

This architecture is suitable for:

* distributed or serverless applications,
* high-traffic media delivery,
* multi-region deployments.

How it works
~~~~~~~~~~~~

.. code-block:: text

   Client
     │
     ├─► PUT {presigned S3 URL}   ──► S3  (direct upload, bypasses app)
     │
     └─► GET https://cdn.example.com/…  ──► CloudFront  ──► S3 (via OAC)
               ├── /public/*   → permanent CF URL, no auth needed
               └── /private/*  → CloudFront signed URL (time-limited)
                                  CF edge validates signature BEFORE fetching from S3

**Origin Access Control (OAC)** ensures the S3 bucket is never accessible
directly — all reads go through CloudFront.  The bucket policy only grants
``s3:GetObject`` to the CloudFront service principal with the distribution ARN
as condition.

For **private assets** the application generates a *CloudFront signed URL*:
the edge node verifies the RSA signature before forwarding the request to S3.
S3 sees a normal OAC-signed request and serves the object.  The private key
never leaves your infrastructure.

.. _download_url_modes:

Download URL modes
~~~~~~~~~~~~~~~~~~

``S3AssetRepositoryConfig`` supports three modes for ``build_download_url``.
They are evaluated in priority order:

.. list-table::
   :header-rows: 1
   :widths: 5 30 15 50

   * - Priority
     - Mode
     - URL expires?
     - When to use
   * - 1 (highest)
     - **CloudFront signed URL** — set ``cf_key_id`` + ``cf_private_key``
     - ✅ configurable TTL
     - Strictest security. Requires a key pair in CloudFront and
       ``trusted_key_groups`` on the private cache behavior.
   * - 2
     - **Plain CloudFront URL** — set ``cf_unsigned_urls=True``
     - ❌ permanent
     - When the CloudFront distribution has *no* viewer-access restriction
       and you rely on OAC to keep S3 private. URL never expires — share
       only with authenticated users at the application layer.
   * - 3 (fallback)
     - **S3 presigned URL** — no CF fields set
     - ✅ configurable TTL
     - Simple setup, no CloudFront key pair needed. Exposes the
       ``s3.amazonaws.com`` domain in URLs.

Terraform
~~~~~~~~~

The snippets below create a production-ready setup: S3 bucket, CloudFront
distribution with OAC, signing key pair + key group (for private signed URLs),
and a Secrets Manager secret for the private key.  Adjust names, regions,
and tags for your environment.

**S3 bucket**

.. code-block:: hcl

   # s3.tf

   resource "aws_s3_bucket" "assets" {
     bucket = var.bucket_name
     tags   = { Environment = var.environment, ManagedBy = "terraform" }
   }

   # Block all direct public access — CloudFront uses OAC.
   resource "aws_s3_bucket_public_access_block" "assets" {
     bucket                  = aws_s3_bucket.assets.id
     block_public_acls       = true
     block_public_policy     = true
     ignore_public_acls      = true
     restrict_public_buckets = true
   }

   # Bucket policy: allow CloudFront OAC to read BOTH prefixes.
   #
   # Two statements are required:
   #   AllowCloudFrontPublic  — public assets served without a token
   #   AllowCloudFrontPrivate — private assets; CloudFront validates the signed
   #                            URL at the edge BEFORE issuing this OAC request
   #
   # The app role needs PutObject / DeleteObject for uploads and deletions.
   resource "aws_s3_bucket_policy" "assets" {
     bucket = aws_s3_bucket.assets.id
     policy = data.aws_iam_policy_document.assets_bucket.json
     depends_on = [aws_cloudfront_distribution.assets]
   }

   data "aws_iam_policy_document" "assets_bucket" {
     statement {
       sid = "AllowCloudFrontPublic"
       principals {
         type        = "Service"
         identifiers = ["cloudfront.amazonaws.com"]
       }
       actions   = ["s3:GetObject"]
       resources = ["${aws_s3_bucket.assets.arn}/public/*"]
       condition {
         test     = "StringEquals"
         variable = "AWS:SourceArn"
         values   = [aws_cloudfront_distribution.assets.arn]
       }
     }

     statement {
       sid = "AllowCloudFrontPrivate"
       principals {
         type        = "Service"
         identifiers = ["cloudfront.amazonaws.com"]
       }
       actions   = ["s3:GetObject"]
       # CloudFront verifies the signed URL signature at the edge;
       # only then does it issue an OAC-authenticated request to S3.
       resources = ["${aws_s3_bucket.assets.arn}/private/*"]
       condition {
         test     = "StringEquals"
         variable = "AWS:SourceArn"
         values   = [aws_cloudfront_distribution.assets.arn]
       }
     }

     statement {
       sid = "AllowAppRole"
       principals {
         type        = "AWS"
         identifiers = [aws_iam_role.app.arn]
       }
       actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:HeadObject"]
       resources = ["${aws_s3_bucket.assets.arn}/*"]
     }
   }

**CloudFront distribution**

.. code-block:: hcl

   # cloudfront.tf

   # ── Origin Access Control ─────────────────────────────────────────────────
   resource "aws_cloudfront_origin_access_control" "assets" {
     name                              = "${var.bucket_name}-oac"
     description                       = "OAC for granite-assets bucket"
     origin_access_control_origin_type = "s3"
     signing_behavior                  = "always"
     signing_protocol                  = "sigv4"
   }

   # ── Signing key pair (for private CloudFront signed URLs) ─────────────────
   #
   # The private key is generated by Terraform, stored in Secrets Manager, and
   # NEVER committed to version control.
   # The public key is uploaded to CloudFront; AWS stores only the public half.

   resource "tls_private_key" "cf_signing" {
     algorithm = "RSA"
     rsa_bits  = 2048
   }

   resource "aws_cloudfront_public_key" "assets" {
     name        = "${var.environment}-assets-signing-key"
     encoded_key = tls_private_key.cf_signing.public_key_pem
   }

   resource "aws_cloudfront_key_group" "assets" {
     name  = "${var.environment}-assets-key-group"
     items = [aws_cloudfront_public_key.assets.id]
   }

   resource "aws_secretsmanager_secret" "cf_private_key" {
     name                    = "${var.environment}/assets/cf-signing-private-key"
     recovery_window_in_days = 7
   }

   resource "aws_secretsmanager_secret_version" "cf_private_key" {
     secret_id     = aws_secretsmanager_secret.cf_private_key.id
     secret_string = tls_private_key.cf_signing.private_key_pem
   }

   # ── Distribution ─────────────────────────────────────────────────────────
   resource "aws_cloudfront_distribution" "assets" {
     enabled         = true
     is_ipv6_enabled = true

     origin {
       domain_name              = aws_s3_bucket.assets.bucket_regional_domain_name
       origin_id                = "s3-assets"
       origin_access_control_id = aws_cloudfront_origin_access_control.assets.id
     }

     # ── Private prefix: requires a valid CloudFront signed URL ────────────
     #
     # trusted_key_groups tells CloudFront to validate the ?Signature= and
     # ?Key-Pair-Id= query parameters on every request matching this pattern.
     # Requests without a valid signature are rejected at the edge (403).
     ordered_cache_behavior {
       path_pattern           = "/private/*"
       allowed_methods        = ["GET", "HEAD"]
       cached_methods         = ["GET", "HEAD"]
       target_origin_id       = "s3-assets"
       viewer_protocol_policy = "redirect-to-https"
       compress               = true

       trusted_key_groups = [aws_cloudfront_key_group.assets.id]

       forwarded_values {
         query_string = false   # CF strips auth params before forwarding to S3
         cookies { forward = "none" }
       }

       min_ttl     = 0
       default_ttl = 0
       max_ttl     = 0   # never cache private responses at the CDN layer
     }

     # ── Public prefix: cached aggressively ────────────────────────────────
     ordered_cache_behavior {
       path_pattern           = "/public/*"
       allowed_methods        = ["GET", "HEAD"]
       cached_methods         = ["GET", "HEAD"]
       target_origin_id       = "s3-assets"
       viewer_protocol_policy = "redirect-to-https"
       compress               = true

       forwarded_values {
         query_string = false
         cookies { forward = "none" }
       }

       min_ttl     = 0
       default_ttl = 86400    # 1 day
       max_ttl     = 31536000 # 1 year
     }

     default_cache_behavior {
       allowed_methods        = ["GET", "HEAD"]
       cached_methods         = ["GET", "HEAD"]
       target_origin_id       = "s3-assets"
       viewer_protocol_policy = "redirect-to-https"
       forwarded_values {
         query_string = false
         cookies { forward = "none" }
       }
     }

     restrictions {
       geo_restriction { restriction_type = "none" }
     }

     viewer_certificate {
       cloudfront_default_certificate = true
       # For a custom domain, replace with:
       # acm_certificate_arn      = aws_acm_certificate.cdn.arn
       # ssl_support_method       = "sni-only"
       # minimum_protocol_version = "TLSv1.2_2021"
     }

     tags = { Environment = var.environment, ManagedBy = "terraform" }
   }

**IAM role for the application**

The application role needs:

* ``s3:PutObject`` / ``s3:DeleteObject`` — to upload and delete assets.
* ``secretsmanager:GetSecretValue`` — to fetch the CF private key at startup.
* ``cloudfront:CreateInvalidation`` — optional, to invalidate CDN cache when
  an asset is updated or deleted.

.. code-block:: hcl

   # iam.tf

   resource "aws_iam_role" "app" {
     name               = "${var.environment}-assets-app"
     assume_role_policy = data.aws_iam_policy_document.assume.json
   }

   data "aws_iam_policy_document" "assume" {
     # Adjust to your compute type: lambda.amazonaws.com, ecs-tasks.amazonaws.com, ec2.amazonaws.com …
     statement {
       actions = ["sts:AssumeRole"]
       principals {
         type        = "Service"
         identifiers = ["lambda.amazonaws.com"]
       }
     }
   }

   resource "aws_iam_role_policy" "app" {
     role   = aws_iam_role.app.id
     policy = data.aws_iam_policy_document.app.json
   }

   data "aws_iam_policy_document" "app" {
     statement {
       actions   = ["s3:PutObject", "s3:DeleteObject", "s3:GetObject", "s3:HeadObject"]
       resources = ["${aws_s3_bucket.assets.arn}/*"]
     }
     statement {
       actions   = ["secretsmanager:GetSecretValue"]
       resources = [aws_secretsmanager_secret.cf_private_key.arn]
     }
     # Optional: CDN cache invalidation when assets change
     statement {
       actions   = ["cloudfront:CreateInvalidation"]
       resources = [aws_cloudfront_distribution.assets.arn]
     }
   }

**Outputs**

.. code-block:: hcl

   output "cdn_domain"             { value = "https://${aws_cloudfront_distribution.assets.domain_name}" }
   output "cf_public_key_id"       { value = aws_cloudfront_public_key.assets.id }
   output "cf_private_key_secret"  { value = aws_secretsmanager_secret.cf_private_key.name }
   output "distribution_id"        { value = aws_cloudfront_distribution.assets.id }

.. important::

   **Key pair length.**  CloudFront requires RSA-2048.  Smaller keys are
   rejected by the API.

   **Key rotation.**  To rotate: generate a new ``tls_private_key``, upload a
   new ``aws_cloudfront_public_key``, add it to the key group (both keys are
   active simultaneously during the rollout), update the secret, restart the
   application, then remove the old key from the key group.

   **Never commit the private key** to version control.  The Terraform state
   file contains it — use a remote backend with encryption at rest (e.g. S3
   with SSE-KMS + DynamoDB state lock).

Repository configuration
~~~~~~~~~~~~~~~~~~~~~~~~~

**Recommended: private key fetched from Secrets Manager at startup**

The application fetches the key once, caches it for the lifetime of the
process, and never exposes it in environment variables or logs.

.. code-block:: python

   import boto3
   import os
   from granite_assets import S3AssetRepositoryConfig, build_asset_repository

   def _fetch_secret(arn: str, region: str) -> str:
       """Fetch a Secrets Manager secret once and cache the result."""
       client = boto3.client("secretsmanager", region_name=region)
       return client.get_secret_value(SecretId=arn)["SecretString"]

   region = os.environ.get("AWS_REGION", "eu-west-1")
   private_key_pem = _fetch_secret(
       os.environ["CF_PRIVATE_KEY_SECRET_ARN"], region
   )

   config = S3AssetRepositoryConfig(
       bucket=os.environ["ASSET_BUCKET"],
       region=region,
       public_base_url=os.environ["CDN_DOMAIN"],  # https://d111….cloudfront.net
       key_prefix=os.environ.get("ASSET_KEY_PREFIX", ""),
       presign_ttl_seconds=3600,
       # CloudFront signed URL fields:
       cf_key_id=os.environ["CF_KEY_ID"],       # KXXXXXXXXXXXXX (from Terraform output)
       cf_private_key=private_key_pem,
   )
   repo = build_asset_repository(config)

**Alternative: inline PEM via environment variable**

Less secure (the key is visible in the process environment) but simpler for
development or environments without Secrets Manager.  Represent newlines as
literal ``\n`` when setting the variable:

.. code-block:: bash

   export CF_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"

.. code-block:: python

   config = S3AssetRepositoryConfig(
       bucket=os.environ["ASSET_BUCKET"],
       region=region,
       public_base_url=os.environ["CDN_DOMAIN"],
       cf_key_id=os.environ["CF_KEY_ID"],
       cf_private_key=os.environ["CF_PRIVATE_KEY"].replace("\\n", "\n"),
   )

**Plain CloudFront URLs (no signing)**

If your distribution has no ``trusted_key_groups`` restriction and you only
need to hide the S3 domain, set ``cf_unsigned_urls=True``.  The URL is
permanent — enforce access control at the application layer.

.. code-block:: python

   config = S3AssetRepositoryConfig(
       bucket=os.environ["ASSET_BUCKET"],
       region=region,
       public_base_url=os.environ["CDN_DOMAIN"],
       cf_unsigned_urls=True,   # plain https://d111….cloudfront.net/private/…
   )

Upload flow
~~~~~~~~~~~

.. code-block:: python

   # 1. Generate a presigned PUT URL
   result = repo.build_upload_url(
       "invoices/inv-001.pdf",
       "application/pdf",
       ttl_seconds=600,
   )
   # result.url     → "https://my-bucket.s3.eu-west-1.amazonaws.com/…?X-Amz-…"
   # result.method  → "PUT"
   # result.headers → {"Content-Type": "application/pdf"}

   # 2. Return the result JSON to the client

   # 3. Client PUTs the file directly to S3 — no server bandwidth used

.. code-block:: javascript

   await fetch(result.url, {
       method: result.method,
       headers: result.headers,
       body: fileBlob,
   });
   // Notify backend that upload is complete
   await fetch('/api/confirm', { method: 'POST',
       body: JSON.stringify({ key: result.key }) });

Download flow
~~~~~~~~~~~~~

.. code-block:: python

   # Public asset — permanent CloudFront URL, no signature needed
   url = repo.build_public_url("public/avatars/user-42.jpg")
   print(url.url)          # https://d111….cloudfront.net/public/avatars/user-42.jpg
   print(url.is_permanent) # True

   # Private asset — CloudFront signed URL (time-limited)
   url = repo.build_download_url("private/invoices/inv-001.pdf", ttl_seconds=300)
   print(url.url)          # https://d111….cloudfront.net/private/…?Expires=…&Signature=…
   print(url.is_permanent) # False
   print(url.expires_at)   # datetime(…, tzinfo=UTC)

.. note::

   CloudFront strips the ``?Expires``, ``?Signature``, and ``?Key-Pair-Id``
   query parameters before forwarding the request to S3 via OAC.  S3 never
   sees the auth parameters — it only sees a valid OAC-signed request.

Environment variables reference
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

+----------------------------------+--------------------------------------------+----------------------------+
| Variable                         | Description                                | Example                    |
+==================================+============================================+============================+
| ``ASSET_BUCKET``                 | S3 bucket name                             | ``prod-my-assets``         |
+----------------------------------+--------------------------------------------+----------------------------+
| ``AWS_REGION``                   | AWS region                                 | ``eu-west-1``              |
+----------------------------------+--------------------------------------------+----------------------------+
| ``CDN_DOMAIN``                   | CloudFront distribution domain             | ``https://d111….cf.net``   |
+----------------------------------+--------------------------------------------+----------------------------+
| ``CF_KEY_ID``                    | CloudFront public key ID                   | ``KXXXXXXXXXXXXX``         |
+----------------------------------+--------------------------------------------+----------------------------+
| ``CF_PRIVATE_KEY_SECRET_ARN``    | Secrets Manager ARN of the private key     | ``arn:aws:secretsmanager…``|
+----------------------------------+--------------------------------------------+----------------------------+
| ``CF_PRIVATE_KEY``               | Inline PEM (alternative to secret ARN)     | ``-----BEGIN RSA …``       |
+----------------------------------+--------------------------------------------+----------------------------+
| ``ASSET_KEY_PREFIX``             | S3 key prefix (e.g. ``assets``)            | ``assets``                 |
+----------------------------------+--------------------------------------------+----------------------------+
| ``ASSET_PRESIGN_TTL_SECONDS``    | Default TTL for signed/presigned URLs      | ``3600``                   |
+----------------------------------+--------------------------------------------+----------------------------+

Troubleshooting
~~~~~~~~~~~~~~~

**403 Access Denied — ``server: AmazonS3`` in response headers**

  The S3 bucket policy does not allow the CloudFront OAC to read the requested
  path.  Check that both ``AllowCloudFrontPublic`` and ``AllowCloudFrontPrivate``
  statements are present in the bucket policy, and that the condition ARN
  matches your distribution.

**403 Access Denied — ``x-cache: Error from cloudfront`` but no S3 header**

  CloudFront rejected the request before it reached S3.  Most likely causes:

  * The URL has expired (``Expires`` timestamp in the past).
  * The signature is invalid — the ``cf_key_id`` used to sign does not belong
    to the ``trusted_key_groups`` configured on the cache behavior.
  * The cache behavior for ``/private/*`` is missing ``trusted_key_groups``.

**403 Access Denied — ``TrustedKeyGroupDoesNotExist`` when updating distribution**

  The key group ID passed to ``trusted_key_groups`` does not exist.  Run
  ``aws cloudfront list-key-groups`` to get the correct ID.

**Signed URL works with ``key_prefix=""`` but 403 with the real prefix**

  The URL path must match the cache behavior pattern.  If ``key_prefix="assets"``
  the generated URL is ``/assets/private/…`` — make sure the CloudFront
  ``ordered_cache_behavior`` uses ``path_pattern = "/assets/private/*"``
  (not just ``/private/*``).


------------------

+------------------------+-------------------------+---------------------------+
|                        | LocalNginx + tusd       | S3 + CloudFront           |
+========================+=========================+===========================+
| **Upload protocol**    | tus (POST + PATCH)      | Presigned PUT             |
+------------------------+-------------------------+---------------------------+
| **Download protocol**  | HTTP (Nginx)            | HTTPS (CloudFront / S3)   |
+------------------------+-------------------------+---------------------------+
| **Resumable uploads**  | Yes (tus protocol)      | No (single-part PUT)      |
+------------------------+-------------------------+---------------------------+
| **Large files**        | Excellent (tus chunking)| Up to 5 GB per PUT        |
+------------------------+-------------------------+---------------------------+
| **Scalability**        | Single host             | Unlimited                 |
+------------------------+-------------------------+---------------------------+
| **Cost**               | Infrastructure only     | Pay-per-request + storage |
+------------------------+-------------------------+---------------------------+
| **Ops complexity**     | Low (two containers)    | Medium (IAM, Terraform)   |
+------------------------+-------------------------+---------------------------+
| **API identical?**     | Yes                     | Yes                       |
+------------------------+-------------------------+---------------------------+

The Python API — ``build_upload_url``, ``build_download_url``, ``save``, etc. —
is **identical** regardless of which backend you choose.  Switching from local
to S3 only requires replacing the configuration dataclass.
