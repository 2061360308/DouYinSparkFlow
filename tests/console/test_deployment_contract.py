import re
import unittest
from pathlib import Path


class DeploymentContractTests(unittest.TestCase):
    def setUp(self):
        self.compose = Path("compose.console.yml").read_text(encoding="utf-8")

    def test_loopback_only_and_no_bps_or_docker_socket(self):
        self.assertIn('${SPARK_WEB_PUBLISH_IP:-127.0.0.1}:8899:8899', self.compose)
        self.assertNotIn("bps", self.compose.lower())
        self.assertNotIn("/var/run/docker.sock", self.compose)

    def test_worker_is_not_published_and_is_resource_limited(self):
        worker = self.compose.split("  spark-worker:", 1)[1].split("\nvolumes:", 1)[0]
        self.assertNotIn("ports:", worker)
        self.assertIn('SPARK_WORKER_CONCURRENCY: "1"', worker)
        self.assertIn("mem_limit: 768m", worker)

    def test_auth_service_is_unpublished_singleton_and_resource_limited(self):
        auth = self.compose.split("  spark-auth:", 1)[1].split("  spark-worker:", 1)[0]
        self.assertIn("<<: *common", auth)
        self.assertIn("command: [python, -m, spark_console.auth_worker]", auth)
        self.assertNotIn("ports:", auth)
        self.assertNotIn("/var/run/docker.sock", auth)
        self.assertIn("mem_limit: 768m", auth)
        self.assertIn("cpus: 1.0", auth)
        self.assertIn('tmpfs: ["/tmp:rw,noexec,nosuid,size=256m"]', auth)

        common = self.compose.split("x-common: &common", 1)[1].split("\nservices:", 1)[0]
        self.assertIn("networks: [spark-private]", common)
        self.assertIn("read_only: true", common)
        self.assertIn("security_opt: [no-new-privileges:true]", common)
        self.assertNotIn("network_mode:", auth)

    def test_host_network_is_limited_to_image_build(self):
        self.assertIn("dockerfile: Dockerfile.console\n    network: host", self.compose)
        service_body = self.compose.split("services:", 1)[1]
        self.assertNotRegex(service_body, r"(?m)^\s{4}network_mode:\s*host")

    def test_example_environment_has_names_but_no_credential_values(self):
        example = Path(".env.console.example").read_text(encoding="utf-8")
        self.assertNotRegex(example, r"(?im)^(?:.*password|.*token|.*secret)=.+$")


if __name__ == "__main__":
    unittest.main()
