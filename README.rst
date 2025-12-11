.. image:: ./docs/element_logo_white_bg.svg
   :height: 60px

**Element Synapse - Matrix homeserver implementation**

|support| |development| |documentation| |license| |pypi| |python|

Synapse is an open source `Matrix <https://matrix.org>`__ homeserver
implementation, written and maintained by `Element <https://element.io>`_.
`Matrix <https://github.com/matrix-org>`__ is the open standard for secure and
interoperable real-time communications. You can directly run and manage the
source code in this repository, available under an AGPL license (or
alternatively under a commercial license from Element).

There is no support provided by Element unless you have a subscription from
Element.

🚀 Getting started
==================

This component is developed and maintained by `Element <https://element.io>`_.
It gets shipped as part of the **Element Server Suite (ESS)** which provides the
official means of deployment.

ESS is a Matrix distribution from Element with focus on quality and ease of use.
It ships a full Matrix stack tailored to the respective use case.

There are three editions of ESS:

- `ESS Community <https://github.com/element-hq/ess-helm>`_ - the free Matrix
  distribution from Element tailored to small-/mid-scale, non-commercial
  community use cases
- `ESS Pro <https://element.io/server-suite>`_ - the commercial Matrix
  distribution from Element for professional use
- `ESS TI-M <https://element.io/server-suite/ti-messenger>`_ - a special version
  of ESS Pro focused on the requirements of TI-Messenger Pro and ePA as
  specified by the German National Digital Health Agency Gematik


🛠️ Standalone installation and configuration
============================================

The Synapse documentation describes `options for installing Synapse standalone
<https://element-hq.github.io/synapse/latest/setup/installation.html>`_. See
below for more useful documentation links.

- `Synapse configuration options <https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html>`_
- `Synapse configuration for federation <https://element-hq.github.io/synapse/latest/federate.html>`_
- `Using a reverse proxy with Synapse <https://element-hq.github.io/synapse/latest/reverse_proxy.html>`_
- `Upgrading Synapse <https://element-hq.github.io/synapse/develop/upgrade.html>`_


🎯 Troubleshooting and support
==============================

🚀 Professional support
-----------------------

Enterprise quality support for Synapse including SLAs is available as part of an
`Element Server Suite (ESS) <https://element.io/pricing>`_ subscription.

If you are an existing ESS subscriber then you can raise a `support request <https://ems.element.io/support>`_
and access the `Element product documentation <https://docs.element.io>`_.

🤝 Community support
--------------------

The `Admin FAQ <https://element-hq.github.io/synapse/latest/usage/administration/admin_faq.html>`_
includes tips on dealing with some common problems. For more details, see
`Synapse's wider documentation <https://element-hq.github.io/synapse/latest/>`_.

For additional support installing or managing Synapse, please ask in the community
support room |room|_ (from a matrix.org account if necessary). We do not use GitHub
issues for support requests, only for bug reports and feature requests.

.. |room| replace:: ``#synapse:matrix.org``
.. _room: https://matrix.to/#/#synapse:matrix.org

.. |docs| replace:: ``docs``
.. _docs: docs

🪪 Identity Servers
===================

Identity servers have the job of mapping email addresses and other 3rd Party
IDs (3PIDs) to Matrix user IDs, as well as verifying the ownership of 3PIDs
before creating that mapping.

**Identity servers do not store accounts or credentials - these are stored and managed on homeservers.
Identity Servers are just for mapping 3rd Party IDs to Matrix IDs.**

This process is highly security-sensitive, as there is an obvious risk of spam if it
is too easy to sign up for Matrix accounts or harvest 3PID data. In the longer
term, we hope to create a decentralised system to manage it (`matrix-doc #712
<https://github.com/matrix-org/matrix-doc/issues/712>`_), but in the meantime,
the role of managing trusted identity in the Matrix ecosystem is farmed out to
a cluster of known trusted ecosystem partners, who run 'Matrix Identity
Servers' such as `Sydent <https://github.com/matrix-org/sydent>`_, whose role
is purely to authenticate and track 3PID logins and publish end-user public
keys.

You can host your own copy of Sydent, but this will prevent you reaching other
users in the Matrix ecosystem via their email address, and prevent them finding
you. We therefore recommend that you use one of the centralised identity servers
at ``https://matrix.org`` or ``https://vector.im`` for now.

To reiterate: the Identity server will only be used if you choose to associate
an email address with your account, or send an invite to another user via their
email address.

🔍 OpenTelemetry Tracing
========================

Synapse supports distributed tracing using OpenTelemetry with OTLP export. This allows you to
trace requests across your Matrix infrastructure and export traces to observability platforms
like Jaeger, Zipkin, or cloud-native solutions.

To enable OpenTelemetry tracing, add the following to your ``homeserver.yaml``:

.. code-block:: yaml

    # OpenTelemetry configuration
    opentelemetry:
      enabled: true
      otlp_endpoint: "http://localhost:4318/v1/traces"  # OTLP HTTP endpoint
      sampler:
        type: ratio              # ratio, always_on, always_off
        ratio: 0.1              # For ratio type: 0.0-1.0 (10% of traces)
      logging: false             # Enable OpenTelemetry debug logging
      resource_attributes:       # Custom resource attributes
        service.version: "1.98.0"
        deployment.environment: "production"
        server.name: "matrix.example.com"
      batch_config:              # Batch processor configuration
        max_export_batch_size: 512
        export_timeout_millis: 30000
        schedule_delay_millis: 5000
        max_queue_size: 2048
      homeserver_whitelist:      # Optional: limit tracing to specific servers
        - ".*\.example\.com"
        - "trusted-server\.org"

**Configuration Options:**

* ``enabled``: Enable/disable OpenTelemetry tracing
* ``otlp_endpoint``: OTLP HTTP endpoint URL (automatically adds /v1/traces if missing)
* ``sampler.type``: Sampling strategy (``ratio``, ``always_on``, ``always_off``)
* ``sampler.ratio``: Sampling probability for ratio-based sampling (0.0-1.0)
* ``logging``: Enable OpenTelemetry SDK debug logging
* ``resource_attributes``: Custom attributes added to all traces
* ``batch_config``: Fine-tune batch export performance
* ``homeserver_whitelist``: Regex patterns for servers to include in tracing

**Popular OTLP Endpoints:**
NOTE: Currently only OTLP over HTTP is supported. The exporter is beta means there can be outstanding issues.
official Synapse only supports Opentracing via legacy Jaeger endpoint which does not work on latest versions of Jaeger v1/v2.
This fork is updated to use latest versions via OpenTelemetry compat layers.
* Jaeger: ``http://localhost:14268/api/traces`` (legacy) or ``http://localhost:4318/v1/traces`` (OTLP)
* Zipkin: ``http://localhost:9411/api/v2/spans``
* OTEL Collector: ``http://localhost:4318/v1/traces``
* Cloud providers: Check your observability platform's OTLP endpoint documentation

📦 Optional Performance Enhancements
====================================

This Synapse implementation includes optional performance optimizations through the
`Matrices-Evolved <https://github.com/alessblaze/Matrices-Evolved/>`_ package, which
provides Rust-accelerated cryptographic operations and caching. The package is licensed
under a `separate license <https://github.com/alessblaze/Matrices-Evolved/blob/main/LICENSE>`_.

Matrices-Evolved can be uninstalled if desired, and Synapse will fall back to using
standard Python libraries for cryptographic operations and caching.
"poetry remove matrices_evolved"
"pip uninstall matrices_evolved"
or by similar means. Also if there is an issue with poetry, do a "poetry install" to clean up dependencies.
post uninstallation of matrices_evolved, restart synapse.

Enhanced Identity Verification can be enabled by setting the following in your homeserver.yaml:

.. code-block:: yaml

    # Matrix-RTC identity verification keys (both keys required)
    matrix_rtc_v2:
      # Option 1: PEM file paths
      server_key_path: "rtc_server.pem"
      client_key_path: "rtc_client_public.pem"
      # Option 2: Base64 encoded raw keys (32 bytes for Ed25519)
      server_key_base64: "xNa5/PQV7BAM6c24+VaqY05GI0GcqWkEVvwsE0P0H34="
      client_key_base64: "AbCdEf1234567890..."
      # Note: Provide either both keys or neither (partial config will cause startup error)

It only works with AMS jwt service, and not with the official lk-jwt-service.
AMS Livekit Service is available at https://github.com/alessblaze/livekit-jwt-service-ams
Additionally, Matrix-RTC identity verification (v2) functionality is available through
the `livekit-jwt-service-ams <https://github.com/alessblaze/livekit-jwt-service-ams>`_
project, which is also under a `separate license <https://github.com/alessblaze/livekit-jwt-service-ams/blob/main/LICENSE>`_.

**Important**: Please read the respective licenses before using these components.
Both the matrices_evolved package and Matrix-RTC v2 verification are **optional**.
Synapse will function normally without them, falling back to standard Python libraries
for cryptographic operations and disabling Matrix-RTC v2 features if not configured.

🛠️ Development
==============

We welcome contributions to Synapse from the community!
The best place to get started is our
`guide for contributors <https://element-hq.github.io/synapse/latest/development/contributing_guide.html>`_.
This is part of our broader `documentation <https://element-hq.github.io/synapse/latest>`_, which includes
information for Synapse developers as well as Synapse administrators.

Developers might be particularly interested in:

* `Synapse's database schema <https://element-hq.github.io/synapse/latest/development/database_schema.html>`_,
* `notes on Synapse's implementation details <https://element-hq.github.io/synapse/latest/development/internal_documentation/index.html>`_, and
* `how we use git <https://element-hq.github.io/synapse/latest/development/git.html>`_.

Alongside all that, join our developer community on Matrix:
`#synapse-dev:matrix.org <https://matrix.to/#/#synapse-dev:matrix.org>`_, featuring real humans!

Copyright and Licensing
=======================

  | Copyright 2014–2017 OpenMarket Ltd
  | Copyright 2017 Vector Creations Ltd
  | Copyright 2017–2025 New Vector Ltd
  | Copyright 2025 Element Creations Ltd

This software is dual-licensed by Element Creations Ltd (Element). It can be
used either:

(1) for free under the terms of the GNU Affero General Public License (as
    published by the Free Software Foundation, either version 3 of the License,
    or (at your option) any later version); OR

(2) under the terms of a paid-for Element Commercial License agreement between
    you and Element (the terms of which may vary depending on what you and
    Element have agreed to).

Unless required by applicable law or agreed to in writing, software distributed
under the Licenses is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the Licenses for the
specific language governing permissions and limitations under the Licenses.

Please contact `licensing@element.io <mailto:licensing@element.io>`_ to purchase
an Element commercial license for this software.


.. |support| image:: https://img.shields.io/badge/matrix-community%20support-success
  :alt: (get community support in #synapse:matrix.org)
  :target: https://matrix.to/#/#synapse:matrix.org

.. |development| image:: https://img.shields.io/matrix/synapse-dev:matrix.org?label=development&logo=matrix
  :alt: (discuss development on #synapse-dev:matrix.org)
  :target: https://matrix.to/#/#synapse-dev:matrix.org

.. |documentation| image:: https://img.shields.io/badge/documentation-%E2%9C%93-success
  :alt: (Rendered documentation on GitHub Pages)
  :target: https://element-hq.github.io/synapse/latest/

.. |license| image:: https://img.shields.io/github/license/element-hq/synapse
  :alt: (check license in LICENSE file)
  :target: LICENSE

.. |pypi| image:: https://img.shields.io/pypi/v/matrix-synapse
  :alt: (latest version released on PyPi)
  :target: https://pypi.org/project/matrix-synapse

.. |python| image:: https://img.shields.io/pypi/pyversions/matrix-synapse
  :alt: (supported python versions)
  :target: https://pypi.org/project/matrix-synapse
