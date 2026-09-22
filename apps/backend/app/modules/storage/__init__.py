"""Binary media storage.

Two real backends behind one small async interface, selected by
``settings.STORAGE_BACKEND``:

* ``local`` — development/test. Files land under ``settings.LOCAL_STORAGE_ROOT``
  and are served by the app itself at ``/media`` (see ``app.main``). Never use
  this in production: the URLs are neither authenticated nor CDN-backed.
* ``s3`` — production (and docker-compose dev with MinIO). A path-style,
  SigV4-signed S3-compatible client implemented on top of ``httpx`` so the app
  has no boto3 dependency. ``public_url`` returns a SigV4 *presigned* GET URL.

Nothing here is a stub: uploads are byte-identical on disk/bucket, deletes
remove them, and presigned URLs resolve.
"""
