container=$(buildah from python:latest)

buildah config --workingdir /src $container
buildah copy $container requirements.txt ./
buildah run $container -- pip install -r requirements.txt
buildah copy $container . .

buildah config --entrypoint '["python", "/src/main.py"]' $container
buildah config --author "Nathan Benner nathan.benner2@nutrien.com" --label name=sw-meraki $container

buildah commit $container sw-meraki