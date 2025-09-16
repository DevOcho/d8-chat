#!/bin/bash

# This script handles the initial setup for Let's Encrypt certificates.
# It solves the chicken-and-egg problem of needing a certificate for Nginx
# to start, but needing Nginx to be running to obtain a certificate.

# Ensure script is run with root privileges if necessary for Docker
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root or with sudo"
  # exit
fi

# Load environment variables from .env file
if [ -f .env ]; then
    export $(cat .env | sed 's/#.*//g' | xargs)
else
    echo ".env file not found. Please create one from example.env"
    exit 1
fi

if [ -z "$D8CHAT_HOSTNAME" ] || [ -z "$CERTBOT_EMAIL" ]; then
    echo "D8CHAT_HOSTNAME and CERTBOT_EMAIL must be set in your .env file."
    exit 1
fi

domains=($D8CHAT_HOSTNAME)
email="$CERTBOT_EMAIL"
data_path="./nginx/certbot"
rsa_key_size=4096
staging=0 # Set to 1 to use the staging environment for testing

if [ -d "$data_path" ]; then
  read -p "Existing data found for $domains. Continue and replace existing certificate? (y/N) " decision
  if [ "$decision" != "Y" ] && [ "$decision" != "y" ]; then
    exit
  fi
fi

if [ ! -e "$data_path/conf/options-ssl-nginx.conf" ] || [ ! -e "$data_path/conf/ssl-dhparams.pem" ]; then
  echo "### Downloading recommended TLS parameters ..."
  mkdir -p "$data_path/conf"
  curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot-nginx/certbot_nginx/_internal/tls_configs/options-ssl-nginx.conf > "$data_path/conf/options-ssl-nginx.conf"
  curl -s https://raw.githubusercontent.com/certbot/certbot/master/certbot/certbot/ssl-dhparams.pem > "$data_path/conf/ssl-dhparams.pem"
  echo
fi

echo "### Creating dummy certificate for $domains ..."
path="/etc/letsencrypt/live/$domains"
mkdir -p "$data_path/conf/live/$domains"
docker compose -f docker-compose.prod.yaml run --rm --entrypoint "\
  openssl req -x509 -nodes -newkey rsa:$rsa_key_size -days 1\
    -keyout '$path/privkey.pem' \
    -out '$path/fullchain.pem' \
    -subj '/CN=localhost'" certbot
echo

echo "### Starting Nginx ..."
docker compose -f docker-compose.prod.yaml up --force-recreate -d nginx
echo

echo "### Deleting dummy certificate for $domains ..."
docker compose -f docker-compose.prod.yaml run --rm --entrypoint "\
  rm -Rf /etc/letsencrypt/live/$domains && \
  rm -Rf /etc/letsencrypt/archive/$domains && \
  rm -Rf /etc/letsencrypt/renewal/$domains.conf" certbot
echo

echo "### Requesting Let's Encrypt certificate for $domains ..."
# Join $domains to -d args
domain_args=""
for domain in "${domains[@]}"; do
  domain_args="$domain_args -d $domain"
done

# Select appropriate email arg
case "$email" in
  "") email_arg="--register-unsafely-without-email" ;;
  *) email_arg="--email $email" ;;
esac

# Enable staging mode if needed
if [ $staging != "0" ]; then staging_arg="--staging"; fi

docker compose -f docker-compose.prod.yaml run --rm --entrypoint "\
  certbot certonly --webroot -w /var/www/certbot \
    $staging_arg \
    $email_arg \
    $domain_args \
    --rsa-key-size $rsa_key_size \
    --agree-tos \
    --force-renewal" certbot
echo

echo "### Reloading Nginx ..."
docker compose -f docker-compose.prod.yaml exec nginx nginx -s reload

echo "### Your SSL certificate has been successfully generated! ###"
echo "### You can now run 'docker compose up -d' to start all services. ###"
