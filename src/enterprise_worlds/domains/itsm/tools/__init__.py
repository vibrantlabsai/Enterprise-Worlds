"""ITSM toolkit — combines all 19 category mixins into one ``ItsmTools`` toolkit.

The toolkit metaclass collects every ``@is_tool`` method across the MRO, giving the full
93-tool ITSM action space. Each mixin lives in its own module mirroring the original MCP's
tool categories.
"""

from enterprise_worlds.domains.itsm.tools._base import ItsmError, ItsmToolsBase
from enterprise_worlds.domains.itsm.tools.change_request_mappings import ChangeRequestMappingToolsMixin
from enterprise_worlds.domains.itsm.tools.changes import ChangeToolsMixin
from enterprise_worlds.domains.itsm.tools.configuration_items import ConfigurationItemToolsMixin
from enterprise_worlds.domains.itsm.tools.groups import GroupToolsMixin
from enterprise_worlds.domains.itsm.tools.incident_affected_cis import IncidentAffectedCIToolsMixin
from enterprise_worlds.domains.itsm.tools.incident_knowledges import IncidentKnowledgeToolsMixin
from enterprise_worlds.domains.itsm.tools.incident_slas import IncidentSLAToolsMixin
from enterprise_worlds.domains.itsm.tools.incident_templates import IncidentTemplateToolsMixin
from enterprise_worlds.domains.itsm.tools.incidents import IncidentToolsMixin
from enterprise_worlds.domains.itsm.tools.knowledge import KnowledgeToolsMixin
from enterprise_worlds.domains.itsm.tools.locations import LocationToolsMixin
from enterprise_worlds.domains.itsm.tools.notification_analysis import NotificationAnalysisToolsMixin
from enterprise_worlds.domains.itsm.tools.notifications import NotificationToolsMixin
from enterprise_worlds.domains.itsm.tools.problems import ProblemToolsMixin
from enterprise_worlds.domains.itsm.tools.service_offerings import ServiceOfferingToolsMixin
from enterprise_worlds.domains.itsm.tools.services import ServiceToolsMixin
from enterprise_worlds.domains.itsm.tools.sla_definitions import SLADefinitionToolsMixin
from enterprise_worlds.domains.itsm.tools.sla_metrics import SLAMetricToolsMixin
from enterprise_worlds.domains.itsm.tools.users import UserToolsMixin


class ItsmTools(
    IncidentToolsMixin,
    UserToolsMixin,
    GroupToolsMixin,
    LocationToolsMixin,
    ConfigurationItemToolsMixin,
    ServiceToolsMixin,
    ServiceOfferingToolsMixin,
    IncidentTemplateToolsMixin,
    ProblemToolsMixin,
    ChangeToolsMixin,
    ChangeRequestMappingToolsMixin,
    KnowledgeToolsMixin,
    IncidentKnowledgeToolsMixin,
    IncidentAffectedCIToolsMixin,
    IncidentSLAToolsMixin,
    SLADefinitionToolsMixin,
    SLAMetricToolsMixin,
    NotificationAnalysisToolsMixin,
    NotificationToolsMixin,
):
    """The full ITSM toolkit (93 tools across 19 categories)."""


__all__ = ["ItsmTools", "ItsmToolsBase", "ItsmError"]
