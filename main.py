import configparser
import itertools
import json
import logging
import os
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Union

import oci
import paramiko
from dotenv import load_dotenv
import requests

# Load environment variables from .env file
load_dotenv('oci.env')

ARM_SHAPE = "VM.Standard.A1.Flex"
E2_MICRO_SHAPE = "VM.Standard.E2.1.Micro"

# Access loaded environment variables and strip white spaces
OCI_CONFIG = os.getenv("OCI_CONFIG", "").strip()
OCT_FREE_AD = os.getenv("OCT_FREE_AD", "").strip()
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "").strip()
WAIT_TIME = int(os.getenv("REQUEST_WAIT_TIME_SECS", "60").strip())
SSH_AUTHORIZED_KEYS_FILE = os.getenv("SSH_AUTHORIZED_KEYS_FILE", "").strip()
OCI_IMAGE_ID = os.getenv("OCI_IMAGE_ID", None).strip() if os.getenv("OCI_IMAGE_ID") else None
OCI_COMPUTE_SHAPE = os.getenv("OCI_COMPUTE_SHAPE", ARM_SHAPE).strip()
SECOND_MICRO_INSTANCE = os.getenv("SECOND_MICRO_INSTANCE", 'False').strip().lower() == 'true'
OCI_SUBNET_ID = os.getenv("OCI_SUBNET_ID", None).strip() if os.getenv("OCI_SUBNET_ID") else None
OPERATING_SYSTEM = os.getenv("OPERATING_SYSTEM", "").strip()
OS_VERSION = os.getenv("OS_VERSION", "").strip()
ASSIGN_PUBLIC_IP = os.getenv("ASSIGN_PUBLIC_IP", "false").strip()
BOOT_VOLUME_SIZE = os.getenv("BOOT_VOLUME_SIZE", "50").strip()
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", 'False').strip().lower() == 'true'
EMAIL = os.getenv("EMAIL", "").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

# Read the configuration from oci_config file
config = configparser.ConfigParser()
try:
    config.read(OCI_CONFIG)
    OCI_USER_ID = config.get('DEFAULT', 'user')
    if OCI_COMPUTE_SHAPE not in (ARM_SHAPE, E2_MICRO_SHAPE):
        raise ValueError(f"{OCI_COMPUTE_SHAPE} is not an acceptable shape")
    env_has_spaces = any(isinstance(confg_var, str) and " " in confg_var
                        for confg_var in [OCI_CONFIG, OCT_FREE_AD, WAIT_TIME,
                                        SSH_AUTHORIZED_KEYS_FILE, OCI_IMAGE_ID, 
                                        OCI_COMPUTE_SHAPE, SECOND_MICRO_INSTANCE, 
                                        OCI_SUBNET_ID, OS_VERSION, NOTIFY_EMAIL, EMAIL,
                                        EMAIL_PASSWORD, DISCORD_WEBHOOK]
                        )
    config_has_spaces = any(' ' in value for section in config.sections() 
                            for _, value in config.items(section))
    if env_has_spaces:
        raise ValueError("oci.env has spaces in values which is not acceptable")
    if config_has_spaces:
        raise ValueError("oci_config has spaces in values which is not acceptable")        

except configparser.Error as e:
    with open("ERROR_IN_CONFIG.log", "w", encoding='utf-8') as file:
        file.write(str(e))
    print(f"Error reading configuration file: {e}", flush=True)

# Set up logging
logging.basicConfig(
    filename="setup_and_info.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logging_step5 = logging.getLogger("launch_instance")
logging_step5.setLevel(logging.INFO)
fh = logging.FileHandler("launch_instance.log")
fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logging_step5.addHandler(fh)

# Set up OCI Config and Clients
oci_config_path = OCI_CONFIG if OCI_CONFIG else "~/.oci/config"
config = oci.config.from_file(oci_config_path)
iam_client = oci.identity.IdentityClient(config)
network_client = oci.core.VirtualNetworkClient(config)
compute_client = oci.core.ComputeClient(config)

IMAGE_LIST_KEYS = [
    "lifecycle_state",
    "display_name",
    "id",
    "operating_system",
    "operating_system_version",
    "size_in_mbs",
    "time_created",
]


def write_into_file(file_path, data):
    with open(file_path, mode="a", encoding="utf-8") as file_writer:
        file_writer.write(data)


def send_email(subject, body, email, password):
    message = MIMEMultipart()
    message["Subject"] = subject
    message["From"] = email
    message["To"] = email
    html_body = MIMEText(body, "html")
    message.attach(html_body)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        try:
            server.starttls()
            server.login(email, password)
            server.sendmail(email, email, message.as_string())
        except smtplib.SMTPException as mail_err:
            logging.error("Error while sending email: %s", mail_err)
            print(f"Error sending email: {mail_err}", flush=True)


def list_all_instances(compartment_id):
    list_instances_response = compute_client.list_instances(compartment_id=compartment_id)
    return list_instances_response.data


def generate_html_body(instance):
    with open('email_content.html', 'r', encoding='utf-8') as email_temp:
        html_template = email_temp.read()
    html_body = html_template.replace('<INSTANCE_ID>', instance.id)
    html_body = html_body.replace('<DISPLAY_NAME>', instance.display_name)
    html_body = html_body.replace('<AD>', instance.availability_domain)
    html_body = html_body.replace('<SHAPE>', instance.shape)
    html_body = html_body.replace('<STATE>', instance.lifecycle_state)
    return html_body


def create_instance_details_file_and_notify(instance, shape=ARM_SHAPE):
    details = [f"Instance ID: {instance.id}",
               f"Display Name: {instance.display_name}",
               f"Availability Domain: {instance.availability_domain}",
               f"Shape: {instance.shape}",
               f"State: {instance.lifecycle_state}",
               "\n"]
    micro_body = 'Two Micro Instances already exist and are running'
    arm_body = '\n'.join(details)
    body = arm_body if shape == ARM_SHAPE else micro_body
    write_into_file('INSTANCE_CREATED', body)

    print(f"SUCCESS: Instance details recorded -> ID: {instance.id}", flush=True)

    if NOTIFY_EMAIL:
        html_body = generate_html_body(instance)
        send_email('OCI INSTANCE CREATED', html_body, EMAIL, EMAIL_PASSWORD)


def notify_on_failure(failure_msg):
    mail_body = (
        "The script encountered an unhandled error and exited unexpectedly.\n\n"
        f"{failure_msg}"
    )
    write_into_file('UNHANDLED_ERROR.log', mail_body)
    if NOTIFY_EMAIL:
        send_email('OCI INSTANCE CREATION SCRIPT: FAILED DUE TO AN ERROR', mail_body, EMAIL, EMAIL_PASSWORD)


def check_instance_state_and_write(compartment_id, shape, states=('RUNNING', 'PROVISIONING'), tries=3):
    print("Checking if instance already exists in account...", flush=True)
    for i in range(tries):
        instance_list = list_all_instances(compartment_id=compartment_id)
        if shape == ARM_SHAPE:
            running_arm_instance = next((instance for instance in instance_list if
                                         instance.shape == shape and instance.lifecycle_state in states), None)
            if running_arm_instance:
                print(f" Found existing active instance: {running_arm_instance.display_name} ({running_arm_instance.id})", flush=True)
                create_instance_details_file_and_notify(running_arm_instance, shape)
                return True
        else:
            micro_instance_list = [instance for instance in instance_list if
                                   instance.shape == shape and instance.lifecycle_state in states]
            if len(micro_instance_list) > 1 and SECOND_MICRO_INSTANCE:
                create_instance_details_file_and_notify(micro_instance_list[-1], shape)
                return True
            if len(micro_instance_list) == 1 and not SECOND_MICRO_INSTANCE:
                create_instance_details_file_and_notify(micro_instance_list[-1], shape)
                return True        
        if tries - 1 > 0:
            time.sleep(10)

    print("No matching active instance found.", flush=True)
    return False


def handle_errors(command, data, log):
    code = data.get("code", "")
    message = data.get("message", "")
    status = data.get("status", 0)

    # Retryable Out of Capacity, Limit Exceeded, or transient API errors
    if code in ("TooManyRequests", "Out of host capacity.", "InternalError", "LimitExceeded") or \
       message in ("Out of host capacity.", "Bad Gateway") or status == 502:
        
        print(f" NOTICE: OCI response [{code or status}] -> {message}. Waiting {WAIT_TIME}s before next try...", flush=True)
        log.info("Command: %s -- Output: %s", command, data)
        time.sleep(max(10, WAIT_TIME))
        return True

    # Critical errors
    print(f" ERROR: Fatal API failure [{code or status}] -> {message}", flush=True)
    failure_msg = '\n'.join([f'{key}: {value}' for key, value in data.items()])
    notify_on_failure(failure_msg)
    raise Exception("Error: %s" % data)


def execute_oci_command(client, method, *args, **kwargs):
    while True:
        try:
            response = getattr(client, method)(*args, **kwargs)
            data = response.data if hasattr(response, "data") else response
            return data
        except oci.exceptions.ServiceError as srv_err:
            data = {"status": srv_err.status,
                    "code": srv_err.code,
                    "message": srv_err.message}
            handle_errors(args, data, logging_step5)


def generate_ssh_key_pair(public_key_file: Union[str, Path], private_key_file: Union[str, Path]):
    print("Generating new SSH key pair...", flush=True)
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(private_key_file)
    write_into_file(public_key_file, (f"ssh-rsa {key.get_base64()} "
                                      f"{Path(public_key_file).stem}_auto_generated"))


def read_or_generate_ssh_public_key(public_key_file: Union[str, Path]):
    public_key_path = Path(public_key_file)

    if not public_key_path.is_file():
        logging.info("SSH key doesn't exist... Generating SSH Key Pair")
        public_key_path.parent.mkdir(parents=True, exist_ok=True)
        private_key_path = public_key_path.with_name(f"{public_key_path.stem}_private")
        generate_ssh_key_pair(public_key_path, private_key_path)

    with open(public_key_path, "r", encoding="utf-8") as pub_key_file:
        ssh_public_key = pub_key_file.read()

    return ssh_public_key


def send_discord_message(message):
    if DISCORD_WEBHOOK:
        payload = {"content": message}
        try:
            response = requests.post(DISCORD_WEBHOOK, json=payload)
            response.raise_for_status()
        except requests.RequestException as e:
            logging.error("Failed to send Discord message: %s", e)


def launch_instance():
    print("Initializing OCI configuration and checking credentials...", flush=True)
    
    # Step 1 - Get TENANCY
    user_info = execute_oci_command(iam_client, "get_user", OCI_USER_ID)
    oci_tenancy = user_info.compartment_id
    print(f"Tenancy ID: {oci_tenancy}", flush=True)

    # Step 2 - Get AD Name
    availability_domains = execute_oci_command(iam_client,
                                                "list_availability_domains",
                                                compartment_id=oci_tenancy)
    oci_ad_name = [item.name for item in availability_domains if
                   any(item.name.endswith(oct_ad) for oct_ad in OCT_FREE_AD.split(","))]
    oci_ad_names = itertools.cycle(oci_ad_name)
    print(f"Targeting Availability Domains: {oci_ad_name}", flush=True)

    # Step 3 - Get Subnet ID
    oci_subnet_id = OCI_SUBNET_ID
    if not oci_subnet_id:
        subnets = execute_oci_command(network_client,
                                      "list_subnets",
                                      compartment_id=oci_tenancy)
        oci_subnet_id = subnets[0].id
    print(f"Using Subnet ID: {oci_subnet_id}", flush=True)

    # Step 4 - Get Image ID
    if not OCI_IMAGE_ID:
        images = execute_oci_command(
            compute_client,
            "list_images",
            compartment_id=oci_tenancy,
            shape=OCI_COMPUTE_SHAPE,
        )
        shortened_images = [{key: json.loads(str(image))[key] for key in IMAGE_LIST_KEYS
                             } for image in images]
        write_into_file('images_list.json', json.dumps(shortened_images, indent=2))
        oci_image_id = next(image.id for image in images if
                            image.operating_system == OPERATING_SYSTEM and
                            image.operating_system_version == OS_VERSION)
    else:
        oci_image_id = OCI_IMAGE_ID
    print(f"Using Image ID: {oci_image_id}", flush=True)

    assign_public_ip = ASSIGN_PUBLIC_IP.lower() in [ "true", "1", "y", "yes" ]
    boot_volume_size = max(50, int(BOOT_VOLUME_SIZE))
    ssh_public_key = read_or_generate_ssh_public_key(SSH_AUTHORIZED_KEYS_FILE)

    # Step 5 - Check existence before starting loop
    instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE, tries=1)

    if OCI_COMPUTE_SHAPE == "VM.Standard.A1.Flex":
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=2, memory_in_gbs=12)
    else:
        shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=1)

    attempt_counter = 0
    print("Beginning creation loop...", flush=True)

    while not instance_exist_flag:
        attempt_counter += 1
        target_ad = next(oci_ad_names)
        print(f"\n[Attempt #{attempt_counter}] Requesting instance in {target_ad}...", flush=True)

        try:
            launch_instance_response = compute_client.launch_instance(
                launch_instance_details=oci.core.models.LaunchInstanceDetails(
                    availability_domain=target_ad,
                    compartment_id=oci_tenancy,
                    create_vnic_details=oci.core.models.CreateVnicDetails(
                        assign_public_ip=assign_public_ip,
                        assign_private_dns_record=True,
                        display_name=DISPLAY_NAME,
                        subnet_id=oci_subnet_id,
                    ),
                    display_name=DISPLAY_NAME,
                    shape=OCI_COMPUTE_SHAPE,
                    availability_config=oci.core.models.LaunchInstanceAvailabilityConfigDetails(
                        recovery_action="RESTORE_INSTANCE"
                    ),
                    instance_options=oci.core.models.InstanceOptions(
                        are_legacy_imds_endpoints_disabled=False
                    ),
                    shape_config=shape_config,
                    source_details=oci.core.models.InstanceSourceViaImageDetails(
                        source_type="image",
                        image_id=oci_image_id,
                        boot_volume_size_in_gbs=boot_volume_size,
                    ),
                    metadata={
                        "ssh_authorized_keys": ssh_public_key},
                )
            )
            if launch_instance_response.status == 200:
                print(f" SUCCESS! Instance launch accepted by Oracle in {target_ad}!", flush=True)
                instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE)

        except oci.exceptions.ServiceError as srv_err:
            if srv_err.code == "LimitExceeded":
                print(f" Warning: Hit LimitExceeded [{srv_err.code}]. Checking if instance was built...", flush=True)
                instance_exist_flag = check_instance_state_and_write(oci_tenancy, OCI_COMPUTE_SHAPE)
                if instance_exist_flag:
                    print(" Instance creation confirmed! Exiting program cleanly.", flush=True)
                    sys.exit(0)
                print(" Instance not found in active list yet. Pausing before continuing retries...", flush=True)

            data = {
                "status": srv_err.status,
                "code": srv_err.code,
                "message": srv_err.message,
            }
            handle_errors("launch_instance", data, logging_step5)


if __name__ == "__main__":
    send_discord_message("🚀 OCI Instance Creation Script: Starting up!")
    try:
        launch_instance()
        send_discord_message("🎉 Success! OCI Instance has been created!")
    except Exception as e:
        error_message = f" Oops! Unexpected error in OCI Script:\n{str(e)}"
        send_discord_message(error_message)
        raise
