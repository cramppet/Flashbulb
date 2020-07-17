import argparse
import json
import logging
import os
import re
import time
import uuid

import boto3
from botocore.exceptions import NoCredentialsError, ClientError

from common.constants import SCREENSHOT_LAMBDA_NAME
from common.utils import check_chromium_layer, check_credentials, check_screenshot_lambda, get_user_response, parse_regions
from urllib.parse import urlparse, urlunparse
import ipaddress


def check_regions(regions, bucket, skip_tests):
    for region in regions:
        logger.info("Checking region {} settings".format(region))
        if not check_chromium_layer(region):
            logger.error("Chromium layer in region {0} is out of date. Try running ./update with region {0}".format(region))
            exit(-1)
        if not check_screenshot_lambda(region):
            logger.error("Screenshot function in region {0} is out of date. Try running ./update with region {0}".format(region))
            exit(-1)
        if not skip_tests:
            if not test_screenshot(region, bucket):
                logger.error("Screenshot function does not appear to be working in {}. Check CloudWatch logs for more information.".format(region))
                exit(-1)


def invoke_screenshot(region, url, bucket, prefix):
    aws_lambda = boto3.client('lambda', region_name=region)
    response = aws_lambda.invoke(
        FunctionName=SCREENSHOT_LAMBDA_NAME,
        InvocationType='Event',
        Payload=json.dumps({'url': url, 'bucket': bucket, 'prefix': prefix}).encode('utf-8')
    )

def invoke_flashbulb(regions, urls, bucket, prefix):
    num_regions = len(regions)
    for i, url in enumerate(urls):
        invoke_screenshot(regions[i % num_regions], url, bucket, prefix)


def get_object_count(bucket, prefix):
    """Return the total number of objects with the given prefix in the given bucket."""
    aws_s3 = boto3.client('s3')
    response = aws_s3.list_objects_v2(
        Bucket=bucket,
        Prefix=prefix
    )
    total_objects = response['KeyCount']
    while response['IsTruncated']:
        response = aws_s3.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
            ContinuationToken=response['NextContinuationToken']
        )
        total_objects += response['KeyCount']
    return total_objects


def wait_for_completion(bucket, prefix, num_urls, silent=False):
    timeout = 60
    start = time.time()
    while time.time() - start < timeout:
        num_files = get_object_count(bucket, prefix)
        if num_files < num_urls:
            if not silent:
                logger.info('Waiting for remaining URLs: {}/{} completed'.format(num_files, num_urls))
            time.sleep(1)
        else:
            if not silent:
                logger.info("All URLs uploaded")
            return True
    if not silent:
        logger.error("Timeout while waiting for URL uploads. Some may have failed.")
    return False


def test_screenshot(region, bucket):
    """Test screenshot lambda is working and can upload to the specified bucket"""
    prefix = str(uuid.uuid4())
    invoke_screenshot(region, 'https://example.com', bucket, prefix)
    result = wait_for_completion(bucket, prefix, 2, True)
    if result:
        aws_s3 = boto3.client('s3')
        aws_s3.delete_object(Bucket=bucket, Key=prefix + '/https-example.com.png')
        aws_s3.delete_object(Bucket=bucket, Key=prefix + '/https-example.com.json')
    return result


def parse_host(line, http_and_https):
    """Return a list of valid hosts given a line containing a URL, IP address, or CIDR."""
    hosts = []
    
    try:
        network = ipaddress.ip_network(line)
        for ip in network.hosts():
            if http_and_https:
                hosts.append('https://' + ip.exploded)
            hosts.append('http://' + ip.exploded)
        return hosts
    except ValueError:
        # Line is not a CIDR address, continue as normal
        pass
    
    if line.find('://') == -1:
        line = 'http://' + line
    
    if config.http_and_https:
        url = urlparse(line)
        url = url._replace(scheme='http')
        hosts.append(urlunparse(url))
        url = url._replace(scheme='https')
        hosts.append(urlunparse(url))
    else:
        hosts.append(line)
    
    return hosts


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('target_list', type=open, help="File of targets to scan")
    parser.add_argument('bucket', help="S3 bucket to upload results")
    parser.add_argument('--regions', type=parse_regions, default="us-east-2", help="A comma-separated list of AWS regions to distribute Flashbulb jobs")
    parser.add_argument('--prefix', default='', help="Prefix to add to filenames in the bucket. By default, files are placed in bucket root.")
    parser.add_argument('--skip-tests', action='store_true', help="Skip the initial test to ensure functions are working properly in each region")
    parser.add_argument('--http-and-https', action='store_true', help="Try to visit every site over http and https")

    # Logging setup
    logger = logging.getLogger('flashbulb')
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)
    
    check_credentials()
    config = parser.parse_args()
    if config.prefix.startswith('/'):
        config.prefix = config.prefix[1:]
    if config.prefix.endswith('/'):
        config.prefix = config.prefix[:-1]
    
    logger.info("Flashbulb is warming up.")

    hosts = []
    for line in config.target_list.readlines():
        line = line.strip()
        if not line:
            continue
        hosts.extend(parse_host(line, config.http_and_https))
    user_input = get_user_response(
        'Flashbulb found {} potential targets. Continue? (y/N)'.format(len(hosts)), ['y', 'n'], 'n')
    if user_input == 'n':
        exit(0)

    # Dedupe
    hosts = list(set(hosts))

    if config.skip_tests:
        logger.warn("Skipping active screenshot function tests.")
    check_regions(config.regions, config.bucket, config.skip_tests)
    logger.info("Checks complete. Safety goggles on!")
    invoke_flashbulb(config.regions, hosts, config.bucket, config.prefix)
    wait_for_completion(config.bucket, config.prefix, len(hosts) * 2)
