# Deploying Orel Server (Oracle Linux, via Docker Hub)

```
Dev machine                       Docker Hub                    Server
build-and-push.ps1  ── push ──>  <namespace>/orel-server  ── pull ──>  Orel server
```

No source code goes on the server. It only has the compose file, nginx config and the secrets.

## Releasing a new version (dev machine)

One-time setup: log in with a Docker Hub access token that has read/write permission.

```powershell
docker login
```

Each release:

```powershell
cd orel-server
powershell -ExecutionPolicy Bypass -File build-and-push.ps1 -Namespace <dockerhub-user> -Version 1.0.0
```

This pushes `<dockerhub-user>/orel-server:1.0.0` and `:latest`.

**Make the Docker Hub repository private.** Go to Docker Hub → Repositories → orel-server → Settings → Visibility. The image contains your application code. The free Docker Hub plan includes only one private repository, and this project needs two (orel-server and cdb-local-server). Either use a paid plan, or ask about putting both images in one repository with different tags.

## One-time server setup

### Install Docker CE

Oracle Linux 8/9 ships Podman by default:

```bash
sudo dnf remove -y podman buildah runc 2>/dev/null
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER        # log out and back in
```

### Log in to Docker Hub

Create a **read-only** access token: Docker Hub → Account settings → Personal access tokens. Then log in on the server:

```bash
docker login -u <dockerhub-user>     # paste the token as the password
```

The server needs outbound HTTPS access to `registry-1.docker.io`, `auth.docker.io` and `production.cloudflare.docker.com`. If it reaches the internet through a proxy, configure the proxy for Docker (`/etc/systemd/system/docker.service.d/http-proxy.conf`).

### Create the deployment directory

```bash
sudo mkdir -p /opt/orel-server/nginx && sudo chown -R $USER /opt/orel-server
```

Copy the deployment files out of the image. The server needs only Docker Hub access for this, not access to the dev machine:

```bash
cd /opt/orel-server
IMG=<dockerhub-user>/orel-server:<version>
docker pull $IMG
docker run --rm $IMG cat /deploy/docker-compose.yml       > docker-compose.yml
docker run --rm $IMG cat /deploy/nginx/nginx.docker.conf  > nginx/nginx.docker.conf
```

On the server, create the secrets. Start from `.env.example` and `endpoints.env.example` and fill in production values:

```bash
cd /opt/orel-server
vi .env            # DOCKERHUB_NAMESPACE=<dockerhub-user>, IMAGE_TAG=1.0.0, OREL_PORT=8080, ...
vi endpoints.env
chmod 600 .env endpoints.env
```

- `POSTGRES_PASSWORD_URLENC` must be the URL-encoded `POSTGRES_PASSWORD`, because the compose file builds `DATABASE_URL` from it.
- In `endpoints.env`, set `CDB_LOCAL_SERVER_URL=http://<DMZ_IP>:8100`.
- `CDB_LOCAL_SERVER_API_KEY` must match one of the DMZ server's `CDB_LOCAL_SERVER_API_KEYS`.
- Set `ECHO_AZURE=false`.

### Open the firewall

```bash
sudo firewall-cmd --permanent --add-port=<OREL_PORT>/tcp && sudo firewall-cmd --reload
```

### Start

```bash
cd /opt/orel-server
docker compose pull
docker compose up -d
docker compose ps                          # wait for (healthy)
curl -s http://127.0.0.1:<OREL_PORT>/api/v1/health
```

On first start, `model-init` downloads the PaddleOCR models from the internet into a volume. This happens only once.

## Updating

```bash
cd /opt/orel-server
vi .env                                    # IMAGE_TAG=<new version>
docker compose pull && docker compose up -d
docker image prune -f                      # remove old image layers
```

If `docker-compose.yml` or `nginx/nginx.docker.conf` changed, copy them out of the new image again (the `docker run --rm ... cat` commands above). Server-specific values such as `OREL_PORT` live in `.env`, so this never overwrites them.

## Rollback

Set `IMAGE_TAG` in `.env` back to the previous version, then run `docker compose up -d`. Postgres data and the OCR models live in named volumes and are not affected.

## Useful commands

```bash
docker compose logs -f app1
docker compose restart nginx
docker compose down          # stops containers, keeps volumes/data
```
