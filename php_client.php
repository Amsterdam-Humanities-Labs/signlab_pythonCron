<?php
/**
 * Client Monitor API - PHP Integration Library
 *
 * Provides the ClientMonitor class for integrating PHP scripts with the
 * Client Monitor system.
 *
 * Usage:
 *     require_once '/home/gomer/pythonCron/php_client.php';
 *
 *     $monitor = new ClientMonitor(
 *         'my-service',
 *         'My Service',
 *         'Service description',
 *         3600
 *     );
 *
 *     $monitor->sendHeartbeatWithStats('success', 'Operation completed', [
 *         'records_processed' => 42
 *     ]);
 */

class ClientMonitor {
    private $apiUrl;
    private $clientId;
    private $clientName;
    private $description;
    private $heartbeatInterval;
    private $hostname;

    /**
     * Initialize the ClientMonitor
     *
     * @param string $clientId Unique identifier for this client
     * @param string $clientName Human-readable name
     * @param string $description Brief description
     * @param int $heartbeatInterval Expected interval between heartbeats in seconds
     * @param string $apiUrl URL of the Client Monitor API endpoint
     */
    public function __construct(
        $clientId,
        $clientName,
        $description = '',
        $heartbeatInterval = 3600,
        $apiUrl = 'https://signcollect.nl/client_monitor_api/api.php'
    ) {
        $this->clientId = $clientId;
        $this->clientName = $clientName;
        $this->description = $description;
        $this->heartbeatInterval = $heartbeatInterval;
        $this->apiUrl = $apiUrl;
        $this->hostname = gethostname();

        // Automatically register on initialization
        $this->register();
    }

    /**
     * Register this client with the API
     *
     * @param array $metadata Optional custom metadata
     * @return bool True if registration was successful, False otherwise
     */
    public function register($metadata = null) {
        try {
            $data = [
                'client_id' => $this->clientId,
                'client_name' => $this->clientName,
                'description' => $this->description,
                'heartbeat_interval' => $this->heartbeatInterval,
                'metadata' => $metadata ?: [
                    'hostname' => $this->hostname,
                    'php_version' => phpversion(),
                    'registered_at' => date('c')
                ]
            ];

            $response = $this->makeRequest('register', $data);

            if ($response && isset($response['success']) && $response['success']) {
                echo "[ClientMonitor] Registered: {$this->clientId}\n";
                return true;
            } else {
                // Check if already exists
                if (isset($response['errors']) && is_array($response['errors'])) {
                    foreach ($response['errors'] as $error) {
                        if (stripos($error, 'already exists') !== false) {
                            echo "[ClientMonitor] Client already registered: {$this->clientId}\n";
                            return true;
                        }
                    }
                }
                echo "[ClientMonitor] Registration failed: " . json_encode($response['errors'] ?? 'Unknown error') . "\n";
                return false;
            }
        } catch (Exception $e) {
            echo "[ClientMonitor] Registration error: " . $e->getMessage() . "\n";
            return false;
        }
    }

    /**
     * Send a heartbeat to update the last_seen timestamp
     *
     * @param array $metadata Optional custom metadata
     * @return bool True if heartbeat was successful, False otherwise
     */
    public function sendHeartbeat($metadata = null) {
        try {
            $data = [
                'client_id' => $this->clientId,
                'metadata' => $metadata ?: [
                    'last_run' => date('c'),
                    'hostname' => $this->hostname
                ]
            ];

            $response = $this->makeRequest('heartbeat', $data);

            if ($response && isset($response['success']) && $response['success']) {
                return true;
            } else {
                echo "[ClientMonitor] Heartbeat failed: " . json_encode($response['errors'] ?? 'Unknown error') . "\n";
                return false;
            }
        } catch (Exception $e) {
            echo "[ClientMonitor] Heartbeat error: " . $e->getMessage() . "\n";
            return false;
        }
    }

    /**
     * Send a heartbeat with status and statistics
     *
     * @param string $status Status of the execution (e.g., 'success', 'error', 'warning')
     * @param string $message Description message
     * @param array $stats Optional statistics array
     * @return bool True if heartbeat was successful, False otherwise
     */
    public function sendHeartbeatWithStats($status, $message, $stats = []) {
        $metadata = array_merge([
            'last_run' => date('c'),
            'hostname' => $this->hostname,
            'status' => $status,
            'message' => $message
        ], $stats);

        return $this->sendHeartbeat($metadata);
    }

    /**
     * Make an HTTP request to the API
     *
     * @param string $action The API action
     * @param array $data The data to send
     * @return array|null The decoded response or null on failure
     */
    private function makeRequest($action, $data) {
        $url = $this->apiUrl . '?action=' . urlencode($action);
        $jsonData = json_encode($data);

        $ch = curl_init($url);
        curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
        curl_setopt($ch, CURLOPT_POST, true);
        curl_setopt($ch, CURLOPT_POSTFIELDS, $jsonData);
        curl_setopt($ch, CURLOPT_HTTPHEADER, [
            'Content-Type: application/json',
            'Content-Length: ' . strlen($jsonData)
        ]);
        curl_setopt($ch, CURLOPT_TIMEOUT, 10);

        $response = curl_exec($ch);
        $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);

        if (curl_errno($ch)) {
            $error = curl_error($ch);
            curl_close($ch);
            throw new Exception("cURL error: $error");
        }

        curl_close($ch);

        // Accept both 200 and 201 as success
        if ($httpCode === 200 || $httpCode === 201) {
            return json_decode($response, true);
        } elseif ($httpCode === 500) {
            // Try to parse response to check for "already exists" error
            $decoded = json_decode($response, true);
            return $decoded;
        } else {
            throw new Exception("HTTP error: $httpCode");
        }
    }
}

// Example usage
if (basename(__FILE__) == basename($_SERVER['PHP_SELF'])) {
    echo "Testing ClientMonitor class...\n";

    $monitor = new ClientMonitor(
        'test-php-client',
        'Test PHP Client',
        'Testing the ClientMonitor PHP library',
        60
    );

    $monitor->sendHeartbeatWithStats(
        'success',
        'Test heartbeat from php_client.php',
        [
            'test_value' => 123,
            'test_string' => 'hello world'
        ]
    );

    echo "Test complete!\n";
}
