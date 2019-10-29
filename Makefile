NODE_BIN=./node_modules/.bin
DIFF_COVER_BASE_BRANCH=master
PYTHON_ENV=py35

help:
	@echo '                                                                                     		'
	@echo 'Makefile for the edX ecommerce project.                                              		'
	@echo '                                                                                     		'
	@echo 'Usage:                                                                               		'
	@echo '    make requirements                 install requirements for local development     		'
	@echo '    make migrate                      apply migrations                               		'
	@echo '    make serve                        start the dev server at localhost:8002         		'
	@echo '    make clean                        delete generated byte code and coverage reports		'
	@echo '    make validate_js                  run JavaScript unit tests and linting          		'
	@echo '    make validate_python              run Python unit tests and quality checks       		'
	@echo '    make fast_validate_python         run Python unit tests (in parallel) and quality checks	'
	@echo '    make quality                      run pycodestyle and Pylint                            		'
	@echo '    make validate                     Run Python and JavaScript unit tests and linting 		'
	@echo '    make html_coverage                generate and view HTML coverage report         		'
	@echo '    make e2e                          run end to end acceptance tests                		'
	@echo '    make extract_translations         extract strings to be translated               		'
	@echo '    make dummy_translations           generate dummy translations                    		'
	@echo '    make compile_translations         generate translation files                     		'
	@echo '    make fake_translations            install fake translations                      		'
	@echo '    make pull_translations            pull translations from Transifex               		'
	@echo '    make update_translations          install new translations from Transifex        		'
	@echo '    make clean_static                 delete compiled/compressed static assets       		'
	@echo '    make static                       compile and compress static assets               		'
	@echo '    make detect_changed_source_translations    check if translation files are up-to-date		'
	@echo '    make check_translations_up_to_date         install fake translations and check if translation files are up-to-date'
	@echo '    make production-requirements      install requirements for production                    '
	@echo '    make validate_translations        validate translations                    '
	@echo '                                                                                     		'

requirements.js:
	npm install
	# Allow root for Docker
	$(NODE_BIN)/bower install --allow-root

requirements: requirements.js
	pip install -r requirements/dev.txt --exists-action w

requirements.tox:
	pip install -r requirements/tox.txt --exists-action w

production-requirements: requirements.js
	pip install -r requirements.txt --exists-action w

migrate: requirements.tox
	tox -e $(PYTHON_ENV)-migrate

serve: requirements.tox
	tox -e $(PYTHON_ENV)-serve

clean:
	find . -name '*.pyc' -delete
	rm -rf coverage htmlcov

clean_static:
	rm -rf assets/* ecommerce/static/build/*

run_check_isort: requirements.tox
	tox -e $(PYTHON_ENV)-check_isort

run_isort: requirements.tox
	tox -e $(PYTHON_ENV)-run_isort

run_pycodestyle: requirements.tox
	tox -e $(PYTHON_ENV)-pycodestyle

run_pep8: run_pycodestyle

run_pylint: requirements.tox
	tox -e $(PYTHON_ENV)-pylint

quality: run_check_isort run_pycodestyle run_pylint

validate_js:
	rm -rf coverage
	$(NODE_BIN)/gulp test
	$(NODE_BIN)/gulp lint

validate_python: clean requirements.tox
	tox -e $(PYTHON_ENV)-tests

fast_validate_python: clean requirements.tox
	DISABLE_ACCEPTANCE_TESTS=True tox -e $(PYTHON_ENV)-tests

validate: validate_python validate_js quality

theme_static: requirements.tox
	tox -e $(PYTHON_ENV)-theme_static

static: requirements.js theme_static requirements.tox
	$(NODE_BIN)/r.js -o build.js
	tox -e $(PYTHON_ENV)-static

html_coverage: requirements.tox
	tox -e $(PYTHON_ENV)-coverage_html

diff_coverage: validate fast_diff_coverage

fast_diff_coverage: requirements.tox
	tox -e $(PYTHON_ENV)-fast_diff_coverage

e2e:
	pytest e2e --html=log/html_report.html --junitxml=e2e/xunit.xml

extract_translations: requirements.tox
	tox -e $(PYTHON_ENV)-extract_translations

dummy_translations: requirements.tox
	tox -e $(PYTHON_ENV)-dummy_translations

compile_translations: requirements.tox
	tox -e $(PYTHON_ENV)-compile_translations

fake_translations: extract_translations dummy_translations compile_translations

pull_translations:
	cd ecommerce && tx pull -af --mode reviewed

push_translations:
	cd ecommerce && tx push -s

update_translations: pull_translations fake_translations

# extract_translations should be called before this command can detect changes
detect_changed_source_translations: requirements.tox
	tox -e $(PYTHON_ENV)-detect_changed_translations

check_translations_up_to_date: fake_translations detect_changed_source_translations

# Validate translations
validate_translations: requirements.tox
	tox -e $(PYTHON_ENV)-validate_translations

export CUSTOM_COMPILE_COMMAND = make upgrade
upgrade: ## update the requirements/*.txt files with the latest packages satisfying requirements/*.in
	pip install -q -r requirements/pip_tools.txt
	pip-compile --upgrade -o requirements/pip_tools.txt requirements/pip_tools.in
	pip-compile --upgrade -o requirements/tox.txt requirements/tox.in
	pip-compile --upgrade -o requirements/base.txt requirements/base.in
	pip-compile --upgrade -o requirements/docs.txt requirements/docs.in
	pip-compile --upgrade -o requirements/e2e.txt requirements/e2e.in
	pip-compile --upgrade -o requirements/test.txt requirements/test.in
	pip-compile --upgrade -o requirements/dev.txt requirements/dev.in
	pip-compile --upgrade -o requirements/production.txt requirements/production.in

# Targets in a Makefile which do not produce an output file with the same name as the target name
.PHONY: help requirements migrate serve clean validate_python quality validate_js validate html_coverage e2e \
	extract_translations dummy_translations compile_translations fake_translations pull_translations \
	push_translations update_translations fast_validate_python clean_static production-requirements
