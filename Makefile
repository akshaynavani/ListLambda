STAGE ?= dev
STACK ?= listlambda-$(STAGE)
REGION ?= us-east-1

.PHONY: install test coverage build deploy deploy-prod outputs publish-web loadtest logs destroy

install:
	python -m pip install -r requirements-dev.txt

test:
	python -m pytest

coverage:
	python -m pytest --cov=listlambda --cov-report=term-missing

build:
	sam build

deploy: build
	sam deploy --config-env default

deploy-prod: build
	sam deploy --config-env prod

outputs:
	aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query "Stacks[0].Outputs" --output table

publish-web:
	aws s3 cp static/index.html s3://$$(aws cloudformation describe-stacks --stack-name $(STACK) \
		--region $(REGION) --query "Stacks[0].Outputs[?OutputKey=='StaticSiteBucketName'].OutputValue" \
		--output text)/index.html --content-type text/html

loadtest:
	API_URL=$$(aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text) \
		./loadtest/run.sh

logs:
	sam logs --stack-name $(STACK) --name ApiFunction --tail

destroy:
	sam delete --stack-name $(STACK) --region $(REGION)
