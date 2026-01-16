![D8-Chat Logo](./app/static/img/d8-chat-logo.png)

# d8-chat

DevOcho Chat is a modern, real-time collaboration hub built with Python, Flask,
Peewee, HTMX, and WebSockets. It is designed to be a self-hostable, open-source
alternative to platforms like Microsoft Teams, Slack, Discord, RocketChat,
Google Chat, WhatsApp, etc.

It was developed with ❤ by [DevOcho](https://www.devocho.com) as a showcase of
our abilities.  Feel free to hire us if you need custom software.

We use d8-chat in our company every day.

*There is a companion mobile app under development.*

## Features
- Real-time Messaging with Presence Indicators
- Public & Private Channels
- Direct Messaging (DMs)
- Threaded Conversations and Replies
- Emojis & Message Reactions
- Message Editing & Deletion
- File Uploads with Image Previews and Carousels
- Markdown Support & Code Snippets with Syntax Highlighting
- User Mentioning (@username, @channel, @here)
- Polls
- Desktop Notifications and Sounds
- Global Search (Messages, Channels, People)
- Single Sign-On (SSO) support via OIDC (e.g., Authentik, Keycloak)
- Dark/Light/System Theme Preference
- And more...

## Quick Start 

*Note: See production installation instructions below if you are ready to deploy.
       the following instructions are for previewing the software.*

### Prerequisites
- [Git](https://git-scm.com/downloads)
- [Docker](https://www.docker.com/get-started)

### 1. Clone the Repository

```sh
git clone https://github.com/DevOcho/d8-chat.git
cd d8-chat
```

### 2. Configure Your Environment

First, copy the example environment file to create your own local configuration.

```sh
cp example.env .env.docker
```

Open `.env.docker` and set `SECRET_KEY`, `POSTGRES_PASSWORD`, and `MINIO_ROOT_PASSWORD`.

### 3. Build and Run

```sh
docker compose up --build -d
```

The application will be available at http://localhost:5001.

The initialization script creates a default `admin` user. The password will be printed in the Docker logs the very first time you start the application. You can view the logs by running: `docker logs <container_name_or_id>` (e.g., `docker logs d8-chat-app-1`).


## Production-Ready deployment with Docker

Getting a local instance of D8-Chat running is simple with Docker and Docker Compose.

### Prerequisites
- [Git](https://git-scm.com/downloads)
- [Docker](https://www.docker.com/get-started)
- [Docker Compose](https://docs.docker.com/compose/install/)


### 1. Clone the Repository

First, clone the repository to your local machine and navigate into the project directory.

```sh
git clone https://github.com/DevOcho/d8-chat.git
cd d8-chat
```

### 2. Configure Your Environment

First, copy the example environment file to create your own local configuration.

```sh
cp example.env .env
```

* `D8CHAT_HOSTNAME`: Change localhost to your server's public IP address or fully qualified domain name (e.g., chat.yourcompany.com).
* `SECRET_KEY`: Generate a new, long, random string.
* `POSTGRES_PASSWORD`: Set a strong, unique password for the database.
* `MINIO_ROOT_PASSWORD`: Set a strong, unique password for the Minio storage admin.

You can generate these passwords using `pwgen 32 -1s` if you'd like.

#### Enabling HTTPS (SSL/TLS) with Let's Encrypt

For a production deployment, it is highly recommended to enable HTTPS. This setup uses Certbot to automatically provision and renew a free SSL certificate from Let's Encrypt.

**Prerequisites for SSL:**
1.  You must have a publicly accessible server (e.g., a VPS from any cloud provider).
2.  You must own a domain name (e.g., `d8-chat.yourcompany.com`).
3.  You must have a DNS "A" record pointing your domain name to your server's public IP address.
4.  Ports 80 and 443 on your server must be open and not blocked by a firewall.

#### One-Time SSL Setup

After you have cloned the repo and configured your `.env` file (especially `D8CHAT_HOSTNAME` and `CERTBOT_EMAIL`), run the following script to obtain your certificate:

```sh
sudo ./init-letsencrypt.sh
```

This script will:

1. Create dummy certificates so Nginx can start.
2. Start the Nginx container.
3. Run Certbot to request a real certificate from Let's Encrypt, which replaces the dummy ones.
4. Reload Nginx with the new certificate.

After this script completes successfully, you can proceed to the main "Build and Run" step.


### 3. Build and Run the Application

With your `.env` file configured, you can build and run all the services with a single command:

```sh
docker compose -f docker-compose.prod.yaml up --build -d
```

* `--build`: This flag tells Docker Compose to build the application image from your local Dockerfile if it doesn't exist or if the code has changed.
* `-d`: This runs the containers in detached mode (in the background).

The entrypoint.sh script will automatically wait for the database to be ready
and run a non-destructive initialization script. It will create tables and
the initial admin user only if they don't already exist. It is safe to
stop and start the application without losing data.

The initialization script creates a default `admin` user. The password will be printed in the Docker logs the very first time you start the application. You can view the logs by running: `docker logs <container_name_or_id>` (e.g., `docker logs d8-chat-app-1`).

### 4. Accessing the Services

Once the containers are running, you can access the services:

* D8-Chat Application: http://localhost
* Minio Admin Console: http://localhost:9001 (Log in with your `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` from your .env file)

### 5, Certificate Renewal

Let's Encrypt certificates are valid for 90 days. You should set up a cron job
on your host server to renew them automatically. You can renew by running:

```sh
docker compose run --rm certbot renew
```

A common approach is to run this command twice a day via a cron job. It will
only renew the certificate if it's close to expiring.

## Stopping the Application

To stop the running containers, use the command:

```sh
docker compose down
```

You can restart them with the following command:

```sh
docker compose up -d
```

## Development instructions

We accept pull requests!  If you have the skills and want to add a feature
then the tech stack and instructions are below.

### Tech Stack
- **Backend:** Python, Flask, Gunicorn
- **Database:** PostgreSQL
- **ORM:** Peewee
- **Real-time Communication:** WebSockets, Valkey (Redis-compatible) for Pub/Sub
- **Frontend:** HTMX, Bootstrap 5, JavaScript
- **File Storage:** Minio (S3-compatible object storage)
- **Containerization:** Docker, Docker Compose

### Local development environment

If you are looking to contribute a Pull Request (thanks) you can run the app
locally with the following commands:

```sh
git clone https://github.com/DevOcho/d8-chat.git
cd d8-chat
virtualenv .
pip3 install -r requirements.txt
```

Create a .env file for development with the following content:

```
# .env - For local development (running python run.py on your host)
SECRET_KEY=your_super_secret_key_change_me
DATABASE_URI=postgresql://d8chat:d8chat@localhost:5432/d8chat

# Optional if you want to test OIDC
#OIDC_CLIENT_ID=
#OIDC_CLIENT_SECRET=
#OIDC_ISSUER_URL=

MINIO_ENDPOINT=localhost:9000
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
MINIO_BUCKET_NAME=d8chat
MINIO_SECURE=False
MINIO_PUBLIC_URL=http://localhost:9000
```

Start the local minio and postgres servers

```sh
docker compose -f docker-compose.dev.yaml up --build -d
```

Initialize and Seed the database and then run the development server.

```sh
python3 init_db.py
python3 seed.py
python3 run.py
```

The site should be available at http://localhost:5001.
