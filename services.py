# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Service configuration for DeutschBuddy.

Wires Vertex AI Memory Bank into the ADK service registry so that both
deployed Agent Runtime and local adk web use the managed Memory Bank instance.
"""

from typing import Any
from google.adk.cli.service_registry import get_service_registry
from google.adk.memory import VertexAiMemoryBankService

PROJECT_ID = "qwiklabs-gcp-03-68a43ea5c71e"
LOCATION = "us-central1"
MEMORY_BANK_ID = "9048795428887199744"

def memory_bank_service_builder(uri: str = "", **kwargs: Any) -> VertexAiMemoryBankService:
    return VertexAiMemoryBankService(
        project=PROJECT_ID,
        location=LOCATION,
        agent_engine_id=MEMORY_BANK_ID,
    )

registry = get_service_registry()
# Register default memory service scheme
registry.register_memory_service("memory", memory_bank_service_builder)
registry.register_memory_service("agentengine", memory_bank_service_builder)
