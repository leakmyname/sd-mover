import boto3
import sys
import os

s3 = boto3.client(
    's3',
    endpoint_url='https://s3.bitiful.net',
    aws_access_key_id='lF2v13wcX8bdC8KnVW7nfdYr',
    aws_secret_access_key='GvCW6X86D8m7h7waFURPLLQh787xSZ8',
    region_name='cn-east-1',
)

local_path = sys.argv[1] if len(sys.argv) > 1 else 'sd_mover_console.exe'
key = os.path.basename(local_path)

print(f"Uploading {local_path} -> s3://shareit/{key}")
s3.upload_file(local_path, 'shareit', key)
print("Done!")
