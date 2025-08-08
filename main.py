import geopy.geocoders
from geopy.geocoders import Nominatim
import meraki
from orionsdk import SwisClient
from functools import partial
import csv
import re
import requests
import argparse
import logging
import json

class MerakiInventory:

  pollers_enabled = {
    'N.Status.ICMP.Native': True,
    'N.Status.SNMP.Native': True,
    'N.ResponseTime.ICMP.Native': True,
    'N.ResponseTime.SNMP.Native': True,
    'N.Details.SNMP.Generic': True,
    'N.Uptime.SNMP.Generic': True,
    'N.Cpu.SNMP.HrProcessorLoad': True,
    'N.Memory.SNMP.NetSnmpReal': True,
    'N.AssetInventory.Snmp.Generic': True,
    'N.Topology_Layer3.SNMP.ipNetToMedia': False,
    'N.Routing.SNMP.Ipv4CidrRoutingTable': False
  }

  def __init__(self, args):
    self.logger = logging.getLogger(__name__)

    self.meraki_dashboard = meraki.DashboardAPI(args.meraki_token)
    self.meraki_orgs = self.meraki_dashboard.organizations.getOrganizations()
    self.swis = SwisClient(args.npm_server, args.npm_username, args.npm_password)
    self.merakiDevices = self.fetchMerakiDevices(models=args.meraki_device_models, productTypes=args.meraki_product_types)
    self.merakiNetworks = self.fetchMerakiNetworks()
    self.monitoredDevices = self.fetchMonitoredDevices()

    geopy.geocoders.options.default_user_agent = 'solarwinds_meraki_inventory'
    geopy.geocoders.options.default_timeout = 20

    self.geolocator = Nominatim()
    self.reverseLookup = partial(self.geolocator.reverse, language="en")
    
    self.deviceLocations = self.loadDeviceLocations()

  # Fetch Device List From Meraki Dashboard
  def fetchMerakiDevices(self, models, productTypes):
    print("Fetching Meraki devices.")
    
    devices = []
    deviceDict = dict()

    if (models is not None):
      # TODO: Handle case when models is a single model or pattern vs. a list of models
      devices = self.meraki_dashboard.organizations.getOrganizationDevices(
        organizationId=self.meraki_orgs[0]['id'],
        perPage=1000,
        total_pages="all",
        model=models
      )
    elif (productTypes is not None):
      devices = self.meraki_dashboard.organizations.getOrganizationDevices(
        organizationId=self.meraki_orgs[0]['id'],
        perPage=1000,
        total_pages="all",
        productTypes=[productTypes]
      )
    else:
      devices = self.meraki_dashboard.organizations.getOrganizationDevices(
        organizationId=self.meraki_orgs[0]['id'],
        perPage=1000,
        total_pages="all"
      )

    for device in devices:
      # TODO: Add Meraki network name in device dictionary
      deviceDict[device['name']] = device

    return deviceDict

  def fetchMerakiNetworks(self):
    networkDict = dict()
    networks = self.meraki_dashboard.organizations.getOrganizationNetworks(
      organizationId=self.meraki_orgs[0]['id'],
      perPage=1000,
      total_pages="all"
    )

    for network in networks:
      networkDict[network["id"]] = network

    return networkDict

  # Fetch Device List From SolarWinds
  def fetchMonitoredDevices(self):
    monitoredDevices = dict()
    results = self.swis.query("SELECT NodeID, NodeName, IP, ObjectSubType, DNS, SysName, Caption, SNMPVersion, Community, Uri FROM Orion.Nodes")
    for row in results['results']:
      monitoredDevices[row['IP']] = row

    return monitoredDevices

  # Compare Lists and Update SolarWinds Where Necessary
  def updateMonitoredDevices(self, SNMPVersion=2, SNMPCommunity=None, SNMPAuthUsername=None, SNMPPassword=None):
    print("Updating monitored devices.")

    # Add any devices that are not currently monitored
    for ip, device in self.merakiDevices.items():
      if (ip not in self.monitoredDevices):
        if (SNMPVersion == "2"):
          self.addNode(
            ip=ip,
            SysName=device["name"],
            SNMPVersion="2",
            SNMPCommunity=SNMPCommunity
          )
        elif (SNMPVersion == "3"):
          self.addNode(
            ip=ip,
            SysName=device["name"],
            SNMPVersion="3",
            SNMPAuthUsername=SNMPAuthUsername,
            SNMPPassword=SNMPPassword
          )
      elif (self.monitoredDevices[ip] != device):  # Fix this line
        self.updateNode(device)

    # Remove any devices that are currently monitored but have been decommissioned
    for ip, device in self.monitoredDevices.items():
      if (ip not in self.merakiDevices):
        self.removeNode(device)

  # Add a node to SolarWinds
  def addNode(self, ip, SysName, SNMPVersion, SNMPCommunity, SNMPAuthUsername, SNMPAuthPassword):
    props = {
      'IPAddress': ip,
      'EngineID': 1,
      'ObjectSubType': 'SNMP',
      'SNMPVersion': SNMPVersion,
      'Community': SNMPCommunity,

      'DNS': '',
      'SysName': SysName
    }
    results = self.swis.create('Orion.Nodes', **props)
    # extract the nodeID from the result
    nodeid = re.search(r'(\d+)$', results).group(0)

    pollers = []
    for k in self.pollers_enabled:
      pollers.append(
          {
              'PollerType': k,
              'NetObject': 'N:' + nodeid,
              'NetObjectType': 'N',
              'NetObjectID': nodeid,
              'Enabled': self.pollers_enabled[k]
          }
      )

    for poller in pollers:
      response = self.swis.create('Orion.Pollers', **poller)

  # Update a node in SolarWinds
  def updateNode(self, device):
    try:
      self.swis.update(device['uri'] + '/CustomProperties', Network=device['network'])
    except Exception as e:
      print(e)
    try:
      self.swis.update(device['uri'] + '/CustomProperties', Country=device['country'])
    except Exception as e:
      print(e)
    try:
      self.swis.update(device['uri'] + '/CustomProperties', State=device['state'])
    except Exception as e:
      print(e)
    try:
      self.swis.update(device['uri'] + '/CustomProperties', City=device['city'])
    except Exception as e:
      print(e)
    try:
      self.swis.update(device['uri'] + '/CustomProperties', Serial=device['serial'])
    except Exception as e:
      print(e)

  # Remove a node from SolarWinds
  def removeNode(self, uri):
    try:
      self.swis.delete(uri)
    except Exception as e:
      print(e)

  def fetchDeviceIP(self, device):
    ipPattern = r"^10.\d{1,3}.\d{1,3}.\d{1,3}$"
    # Use "lapIp" if present
    if ("lanIp" in device):
      ip = device["lanIp"]
    else:
      try:
        vlans = self.meraki_dashboard.appliance.getNetworkApplianceVlans(device["networkId"])
        # Use the IP address for the "Enterprise Client Network" VLAN if present
        for vlan in vlans:
          if (vlan["name"] == "Enterprise Client Network"):
            return vlan["applianceIp"]
        # Else use the IP address from one of the VLANs that is within 10.0.0.0/8
        if (ip == ""):
          for vlan in vlans:
            if (re.match(ipPattern, vlan["applianceIp"])):
              return vlan["applianceIp"]
      except:
        print(f"ERROR : Device {device["name"]} does not have VLANs")
      return ""

  def discoverDevices(self):
    bulkList = []
    
    # Add any devices that are not currently monitored
    for name, device in self.merakiDevices.items():
      # Add only 10 devices at a time
      # if (count > 1000):
      #   break

      ip = self.fetchDeviceIP(device)

      # If the IP could not be found continue to the next device
      if (ip == ""):
        continue

      # If the node is not yet monitored add it to the bulk list
      if (ip not in self.monitoredDevices):
        bulkList.append({'Address': ip})
      elif (self.monitoredDevices[ip]['ObjectSubType'] != 'SNMP'):
        self.removeNode(self.monitoredDevices[ip]['Uri'])
        bulkList.append({'Address': ip})
      # else:
      #   print(self.monitoredDevices[ip])

    # TODO: Remove any devices that are currently monitored but have been decommissioned
    for ip, device in self.monitoredDevices.items():
      if (ip not in self.merakiDevices):
        pass
        # removeNode(swis, device)

    corePluginContext = {
      'BulkList': bulkList,
      'Credentials': [
        {
          'CredentialID': 12,
          'Order': 1
        },
        {
          'CredentialID': 15,
          'Order': 2
        }
      ],
      'WmiRetriesCount': 0,
      'WmiRetryIntervalMiliseconds': 1000
    }

    corePluginConfig = self.swis.invoke('Orion.Discovery', 'CreateCorePluginConfiguration', corePluginContext)
    
    discoveryProfile = {
      'Name': 'Meraki MX Discovery',
      'EngineID': 1,
      'JobTimeoutSeconds': 3600,
      'SearchTimeoutMiliseconds': 5000,
      'SnmpTimeoutMiliseconds': 5000,
      'SnmpRetries': 2,
      'RepeatIntervalMiliseconds': 1800,
      'SnmpPort': 161,
      'HopCount': 0,
      'PreferredSnmpVersion': 'SNMP2c',
      'DisableIcmp': False,
      'AllowDuplicateNodes': False,
      'IsAutoImport': True,
      'IsHidden': False,
      'PluginConfigurations': [{'PluginConfigurationItem': corePluginConfig}]
    }

    result = self.swis.invoke('Orion.Discovery', 'StartDiscovery', discoveryProfile)

  def saveDeviceLocations(self, device):
    with open('locations.csv', 'a', newline='') as locationCsv:
      fieldnames = ['serial', 'lat','lng', 'country', 'state', 'city', 'address']
      writer = csv.DictWriter(locationCsv, delimiter=',', quotechar='|', quoting=csv.QUOTE_MINIMAL, fieldnames=fieldnames)
      writer.writerow(device)

  def loadDeviceLocations(self):
    deviceLocations = dict()
    fieldnames = ['serial', 'lat','lng', 'country', 'state', 'city', 'address']
    try:
      with open('locations.csv', newline='') as locationCsv:
        reader = csv.DictReader(locationCsv, delimiter=',', quotechar='|', fieldnames=fieldnames)
        for row in reader:
          deviceLocations[row['ip']] = row
    except FileNotFoundError:
      return {}
    return deviceLocations

  def lookupDeviceLocation(self, device):
    if device['serial'] in self.deviceLocations:
      return self.deviceLocations[device['serial']]
    location = self.reverseLookup((device['lat'], device['lng']))
    city = location
    if ('city' in location.raw['address']):
      city = location.raw['address']['city']
    elif ('village' in location.raw['address']):
      city = location.raw['address']['village']
    elif ('municipality' in location.raw['address']):
      city = location.raw['address']['municipality']
    elif ('county' in location.raw['address']):
      city = location.raw['address']['county']
    
    locationData = {
      'serial': device['serial'],
      'lat': device['lat'],
      'lng': device['lng'],
      'address': location,
      'country': location.raw['address']['country'],
      'state': location.raw['address']['state'],
      'city': city
    }

    self.saveDeviceLocations(locationData)
    return locationData

  def dryRun(self):
    count = 0
    for name, device in self.merakiDevices.items():
      if (count > 10):
        break
      count += 1

      ip = self.fetchDeviceIP(device)

      # If the IP could not be found continue to the next device
      if (ip == ""):
        print(f"Count not find IP for device {device['name']}")
        continue

      try:
        location = self.lookupDeviceLocation(device)
      except:
        continue

      if (ip not in self.monitoredDevices):
        # Add device to be discovered
        continue

      deviceInfo = {
        'name': name,
        'serial': device['serial'],
        'mac': device['mac'],
        'ip': ip,
        'model': device['model'],
        'network': self.merakiNetworks[device['networkId']],
        'lat': device['lat'],
        'lng': device['lng'],
        'country': location['country'],
        'province': location['province'],
        'address': location['address'],
        'city': location['city'],
        'uri': self.monitoredDevices[ip]["uri"]
      }

      print(deviceInfo)

  def updateDevices(self):
    count = 0
    for name, device in self.merakiDevices.items():
      # if (count > 5):
      #   break
    
      count += 1

      ip = self.fetchDeviceIP(device)

      # If the IP could not be found continue to the next device
      if (ip == ""):
        print(f"Count not find IP for device {device['name']}")
        continue

      if (ip not in self.monitoredDevices):
        # TODO: Add device to be discovered
        print("device not in monitored devices")
        continue

      location = {}
      try:
        location = self.lookupDeviceLocation(device)
      except:
        print("Error getting device location. Skipping.")
        continue

      deviceInfo = {
        'name': name,
        'serial': device['serial'],
        'mac': device['mac'],
        'ip': ip,
        'model': device['model'],
        'network': self.merakiNetworks[device['networkId']],
        'lat': device['lat'],
        'lng': device['lng'],
        'country': location['country'],
        'state': location['state'],
        'address': location['address'],
        'city': location['city'],
        'uri': self.monitoredDevices[ip]["Uri"]
      }

      self.updateNode(deviceInfo)

def main():
  # Parse program arguments
  parser = argparse.ArgumentParser(
    prog="SwMerakiInventory",
    description="This program updates SolarWinds inventory with devices from Meraki Dashboard."
  )
  parser.add_argument("--meraki_token", help="Meraki dashboard API token.")
  parser.add_argument("--meraki_device_models", help="Meraki device model to add to SolarWinds.")
  parser.add_argument(
    "--meraki_product_types",
    help="Meraki device product types to add to SolarWinds. Valid types are wireless, appliance, switch, systemsManager, camera, cellularGateway, sensor, wirelessController, campusGateway, and secureConnect.",
    choices=["wireless", "appliance", "switch", "systemsManager", "camera", "cellularGateway", "sensor", "wirelessController", "campusGateway", "secureConnect"])
  parser.add_argument("--npm_server", help="SolarWinds server address.")
  parser.add_argument("--npm_username", help="SolarWinds username.")
  parser.add_argument("--npm_password", help="SolarWinds password.")
  parser.add_argument("--snmp_version", help="SNMP version for monitoring devices (2 or 3).")
  parser.add_argument("--snmp_community", help="SNMP community for monitoring devices using SNMPv2c.")
  parser.add_argument("--snmp_auth_username", help="SNMP authentication username for monitoring devices using SNMPv3.")
  parser.add_argument("--snmp_auth_password", help="SNMP authentication password for monitoring devices using SNMPv3.")
  parser.add_argument("--mode", help="The mode to run this script in. Options are \"add\", \"discover\", \"update\" and \"dry\".", choices=["add", "discover", "dry", "update"])
  parser.add_argument("--limit", help="Limit the number of devices to use (for testing).")
  args = parser.parse_args()

  merakiInventory = MerakiInventory(args)
  
  logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
      logging.FileHandler("meraki_query.log")
    ]
  )

  if (args.mode == "add"):
    merakiInventory.updateMonitoredDevices()
  elif (args.mode == "discover"):
    merakiInventory.discoverDevices()
  elif (args.mode == "update"):
    print("Updating devices")
    merakiInventory.updateDevices()
  elif (args.mode == "dry"):
    merakiInventory.dryRun()
  else:
    print(f"Error. Invalid argument for mode: {args.mode}")

requests.packages.urllib3.disable_warnings()

if __name__=="__main__":
  main()
