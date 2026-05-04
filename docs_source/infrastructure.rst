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
     ├─► PUT {presigned S3 URL}  ──► S3 (direct, bypasses app server)
     │
     └─► GET https://cdn.example.com/…  ──► CloudFront  ──► S3
                 ├── /public/*  → no OAC restriction (public bucket policy)
                 └── /private/* → signed CloudFront URL (or OAC + app-level auth)

Public assets are served via CloudFront from an S3 prefix with a public-read
bucket policy.  Private assets are uploaded with a presigned PUT URL (valid for
a configurable TTL) and downloaded via either a presigned S3 URL or a signed
CloudFront URL.

Terraform
~~~~~~~~~

The Terraform snippets below create a minimal production-ready setup: one S3
bucket, a CloudFront distribution with Origin Access Control, and the required
IAM policy for your application role.  Adjust names, regions, and tags for your
environment.

.. code-block:: hcl

   # ─── variables.tf ────────────────────────────────────────────────────────────

   variable "bucket_name" {
     description = "S3 bucket name for asset storage (must be globally unique)"
     type        = string
   }

   variable "region" {
     description = "AWS region"
     type        = string
     default     = "eu-west-1"
   }

   variable "environment" {
     description = "Environment tag (production, staging, …)"
     type        = string
     default     = "production"
   }

.. code-block:: hcl

   # ─── s3.tf ───────────────────────────────────────────────────────────────────

   resource "aws_s3_bucket" "assets" {
     bucket = var.bucket_name

     tags = {
       Environment = var.environment
       ManagedBy   = "terraform"
     }
   }

   # Block all public access — CloudFront uses OAC; app uses presigned URLs.
   resource "aws_s3_bucket_public_access_block" "assets" {
     bucket                  = aws_s3_bucket.assets.id
     block_public_acls       = true
     block_public_policy     = true
     ignore_public_acls      = true
     restrict_public_buckets = true
   }

   # Bucket policy: allow CloudFront OAC to read and the app role to write.
   resource "aws_s3_bucket_policy" "assets" {
     bucket = aws_s3_bucket.assets.id
     policy = data.aws_iam_policy_document.assets_bucket.json
   }

   data "aws_iam_policy_document" "assets_bucket" {
     # CloudFront OAC read access (GetObject)
     statement {
       principals {
         type        = "Service"
         identifiers = ["cloudfront.amazonaws.com"]
       }
       actions   = ["s3:GetObject"]
       resources = ["${aws_s3_bucket.assets.arn}/*"]
       condition {
         test     = "StringEquals"
         variable = "AWS:SourceArn"
         values   = [aws_cloudfront_distribution.assets.arn]
       }
     }

     # Application role: full read + write access
     statement {
       principals {
         type        = "AWS"
         identifiers = [aws_iam_role.app.arn]
       }
       actions = [
         "s3:GetObject",
         "s3:PutObject",
         "s3:DeleteObject",
         "s3:HeadObject",
       ]
       resources = ["${aws_s3_bucket.assets.arn}/*"]
     }
   }

.. code-block:: hcl

   # ─── cloudfront.tf ───────────────────────────────────────────────────────────

   resource "aws_cloudfront_origin_access_control" "assets" {
     name                              = "${var.bucket_name}-oac"
     description                       = "OAC for granite-assets bucket"
     origin_access_control_origin_type = "s3"
     signing_behavior                  = "always"
     signing_protocol                  = "sigv4"
   }

   resource "aws_cloudfront_distribution" "assets" {
     enabled             = true
     is_ipv6_enabled     = true
     default_root_object = ""

     origin {
       domain_name              = aws_s3_bucket.assets.bucket_regional_domain_name
       origin_id                = "s3-assets"
       origin_access_control_id = aws_cloudfront_origin_access_control.assets.id
     }

     # Public prefix — cached aggressively, no auth needed.
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

       # Long TTL for public assets (they are content-addressed or versioned).
       min_ttl     = 0
       default_ttl = 86400     # 1 day
       max_ttl     = 31536000  # 1 year
     }

     # Private prefix — short TTL, no caching of auth state.
     ordered_cache_behavior {
       path_pattern           = "/private/*"
       allowed_methods        = ["GET", "HEAD"]
       cached_methods         = ["GET", "HEAD"]
       target_origin_id       = "s3-assets"
       viewer_protocol_policy = "redirect-to-https"
       compress               = true

       forwarded_values {
         query_string = true   # preserve presigned URL query params
         cookies { forward = "none" }
       }

       min_ttl     = 0
       default_ttl = 0
       max_ttl     = 0  # never cache private responses at the CDN layer
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

     tags = {
       Environment = var.environment
       ManagedBy   = "terraform"
     }
   }

   # Output the CDN domain to use as public_base_url
   output "cdn_domain" {
     value = "https://${aws_cloudfront_distribution.assets.domain_name}"
   }

.. code-block:: hcl

   # ─── iam.tf ──────────────────────────────────────────────────────────────────

   resource "aws_iam_role" "app" {
     name               = "${var.environment}-granite-assets-app"
     assume_role_policy = data.aws_iam_policy_document.assume.json
   }

   data "aws_iam_policy_document" "assume" {
     # Adjust principal to match your compute type (Lambda, ECS task, EC2).
     statement {
       actions = ["sts:AssumeRole"]
       principals {
         type        = "Service"
         identifiers = ["lambda.amazonaws.com"]
       }
     }
   }

Repository configuration
~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   import os
   from granite_assets import S3AssetRepositoryConfig, build_asset_repository

   config = S3AssetRepositoryConfig(
       bucket=os.environ["ASSET_BUCKET"],
       region=os.environ.get("AWS_REGION", "eu-west-1"),
       # CDN domain output by Terraform → stable, cacheable public URLs.
       public_base_url=os.environ.get("CDN_DOMAIN"),
       presign_ttl_seconds=900,          # 15 min for uploads/downloads
       key_prefix=os.environ.get("ASSET_KEY_PREFIX", ""),
       # Leave access_key_id / secret_access_key as None
       # to use the IAM role credential chain (recommended).
   )
   repo = build_asset_repository(config)

Upload flow
~~~~~~~~~~~

.. code-block:: python

   # 1. Your API generates a presigned upload URL
   result = repo.build_upload_url(
       "invoices/inv-001.pdf",
       "application/pdf",
       ttl_seconds=600,
   )
   # result.url     → "https://my-bucket.s3.eu-west-1.amazonaws.com/…?X-Amz-…"
   # result.method  → "PUT"
   # result.headers → {"Content-Type": "application/pdf"}

   # 2. Return the result JSON to the client

   # 3. The client PUTs the file directly to S3 — no server bandwidth used

.. code-block:: javascript

   // Browser / Node.js
   await fetch(result.url, {
       method: result.method,
       headers: result.headers,
       body: fileBlob,
   });

   // Notify your backend that the upload is complete
   await fetch('/api/confirm', {
       method: 'POST',
       body: JSON.stringify({ key: result.key }),
   });

Download flow
~~~~~~~~~~~~~

.. code-block:: python

   # Public asset — permanent CloudFront URL
   url = repo.build_public_url("avatars/user-42.jpg")
   print(url.url)          # https://d111111abcdef8.cloudfront.net/public/avatars/…
   print(url.is_permanent) # True

   # Private asset — presigned S3 URL (time-limited)
   url = repo.build_download_url("invoices/inv-001.pdf", ttl_seconds=300)
   print(url.url)          # https://my-bucket.s3.eu-west-1.amazonaws.com/…?X-Amz-…
   print(url.is_permanent) # False

S3 event notifications (optional)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Instead of having the client call a ``/confirm`` endpoint, you can configure
S3 event notifications to trigger a Lambda or SQS message when an object is
created.  This is more robust — the event fires even if the client disconnects
before calling your API.

.. code-block:: hcl

   # s3.tf — add to the aws_s3_bucket resource
   resource "aws_s3_bucket_notification" "uploads" {
     bucket = aws_s3_bucket.assets.id

     lambda_function {
       lambda_function_arn = aws_lambda_function.post_upload.arn
       events              = ["s3:ObjectCreated:*"]
       filter_prefix       = ""   # all prefixes; narrow as needed
     }
   }

----

Choosing a Backend
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
