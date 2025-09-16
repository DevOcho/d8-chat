# d8-chat
DevOcho Chat is a modern, real-time collaboration hub built with Python, Flask, Peewee, HTMX, and WebSockets. It is designed to be a self-hostable, open-source alternative to platforms like Slack.

## Features
- Real-time Messaging
- Public & Private Channels
- Direct Messaging
- Threaded Conversations
- Emojis & Reactions
- Message Editing & Deletion
- Markdown & Code Snippets with Syntax Highlighting
- User Presence Status (Online, Away, Busy)
- Image Uploads with Image Previews
- SSO support (OIDC)
- And more...

## Quick Start (Local Development or testing)

### 1. Clone the Repository

```sh
git clone https://github.com/DevOcho/d8-chat.git
cd d8-chat
```

### 2. Configure Your Environment

Open `.env` and set `SECRET_KEY`, `POSTGRES_PASSWORD`, and `MINIO_ROOT_PASSWORD`.

### 3. Build and Run

```sh
docker compose -f docker-compose.dev.yaml up --build -d
```

The application will be available at http://localhost:5001.

## Production-Ready deployment with Docker

Getting a local instance of D8-Chat running is simple with Docker and Docker Compose.

### Prerequisites
- 
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
docker compose up --build -d
```

* `--build`: This flag tells Docker Compose to build the application image from your local Dockerfile if it doesn't exist or if the code has changed.
* `-d`: This runs the containers in detached mode (in the background).

The entrypoint.sh script will automatically wait for the database to be ready
and run a non-destructive initialization script. It will create tables and
the initial admin user only if they don't already exist. It is safe to
stop and start the application without losing data.

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

You can restart them with the followig:

```sh
docker compose up -d
```
