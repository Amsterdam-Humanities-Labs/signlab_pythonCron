<?php
/**
 * MySQL Backup Script with Advanced Retention Policy
 *
 * - Keeps hourly backups for the past two weeks.
 * - Keeps one daily backup for backups older than two weeks.
 *
 * Ensure this script is run hourly via a cron job.
 */

require_once '/home/gomer/pythonCron/php_client.php';

// Initialize Client Monitor
$clientMonitor = new ClientMonitor(
    'mysql-backup',
    'MySQL Backup',
    'Creates MySQL database backups with retention policy',
    3600  // 60 minutes
);

$backupStats = [
    'tables_backed_up' => 0,
    'hourly_backups' => 0,
    'daily_backups' => 0,
    'tables_skipped' => 0,
    'backups_deleted' => 0,
    'errors' => 0
];

try {

// ======================= Configuration ======================= //

// Include the MySQL configuration file (use absolute path for scheduler compatibility)
include('/web/mysql_config.php');

// Define the database name
$database = 'admin_gebarenoverleg';

// Define the backup directory path
// $backupDir = "/web/sqlBackups/";
$backupDir = "/web/gebarenoverleg_media/studioFiles/sqlBackups/";
// Define the retention periods
define('RETENTION_HOURLY_DAYS', 14); // Days to keep hourly backups
define('RETENTION_DAILY_DAYS', 365); // Days to keep daily backups (optional)

// MySQL dump options
$mysqldumpOptions = '--no-tablespaces'; // Avoid tablespace dump which requires PROCESS privilege

// Credentials are passed to mysqldump through a 0600 defaults file rather than
// --password= on the command line, which would expose them in `ps` output to
// every user on this host.
$mysqlDefaultsFile = tempnam(sys_get_temp_dir(), 'mysqldump_');
if ($mysqlDefaultsFile === false) {
    die("Failed to create temporary MySQL defaults file\n");
}
chmod($mysqlDefaultsFile, 0600);
file_put_contents(
    $mysqlDefaultsFile,
    "[client]\nuser=\"" . addslashes($username) . "\"\npassword=\"" . addslashes($password) . "\"\n"
);
// Remove it however the script exits.
register_shutdown_function(function () use ($mysqlDefaultsFile) {
    if (file_exists($mysqlDefaultsFile)) {
        unlink($mysqlDefaultsFile);
    }
});

// ===================== Setup Backup Directory ===================== //

// Ensure the backup directory exists; if not, attempt to create it
if (!is_dir($backupDir)) {
    if (!mkdir($backupDir, 0755, true)) {
        die("Failed to create backup directory: $backupDir\n");
    }
}

// ======================= Database Connection ======================= //

// Create a new MySQLi connection
$mysqli = new mysqli($servername, $username, $password, $database);

// Check for a successful connection
if ($mysqli->connect_error) {
    die("Connection failed: " . $mysqli->connect_error . "\n");
}

// ======================= Retrieve All Tables ======================= //

$tables = [];
$result = $mysqli->query("SHOW TABLES");

if ($result) {
    while ($row = $result->fetch_array()) {
        $tables[] = $row[0];
    }
    $result->free();
} else {
    die("Error fetching table list: " . $mysqli->error . "\n");
}

// Close the MySQL connection as it's no longer needed
$mysqli->close();

// ======================= Create Backups ======================= //

// Define tables to skip during backup (too large for any backup)
$skipTables = [
    'hand_pose_files',
    'hand_pose_fingertip_distances',
    'hand_pose_finger_distances',
    'hand_pose_finger_spreads'
];

// Define critical tables for HOURLY backups
$hourlyTables = [
    'CameraRecords',
    'form_data',
    'matched_transcriptions',
    'sentences'
];

$dateTime = date('Y-m-d_H-i-s'); // Current date and time
$todayDate = date('Y-m-d'); // Current date only

foreach ($tables as $table) {
    // Skip backup for large tables
    if (in_array($table, $skipTables)) {
        echo "⏭️ Skipping backup for large table: $table\n";
        $backupStats['tables_skipped']++;
        continue;
    }

    // For non-hourly tables, check if we already have a backup today
    if (!in_array($table, $hourlyTables)) {
        $todayBackupPattern = $backupDir . "{$table}_backup_{$todayDate}_*.sql";
        $existingTodayBackups = glob($todayBackupPattern);

        if (!empty($existingTodayBackups)) {
            echo "⏭️ Daily backup already exists for table: $table (backup interval: daily)\n";
            $backupStats['tables_skipped']++;
            continue;
        } else {
            echo "📅 Creating daily backup for table: $table\n";
        }
    } else {
        echo "⏰ Creating hourly backup for table: $table\n";
    }

    // Define the backup file path for the current table
    $backupFileName = "{$table}_backup_{$dateTime}.sql";
    $backupFilePath = $backupDir . $backupFileName;

    // Construct the mysqldump command with proper escaping and no-tablespaces option
    $command = "mysqldump --defaults-extra-file=" . escapeshellarg($mysqlDefaultsFile) .
               " -h " . escapeshellarg($servername) .
               " " . $mysqldumpOptions .
               " " . escapeshellarg($database) .
               " " . escapeshellarg($table) .
               " > " . escapeshellarg($backupFilePath);

    // Execute the mysqldump command
    exec($command, $output, $return_var);

    // Check if the backup file was created successfully
    if ($return_var === 0 && file_exists($backupFilePath)) {
        echo "✅ Backup successful for table: $table at $backupFilePath\n";
        $backupStats['tables_backed_up']++;

        // Track hourly vs daily backups
        if (in_array($table, $hourlyTables)) {
            $backupStats['hourly_backups']++;
        } else {
            $backupStats['daily_backups']++;
        }
    } else {
        echo "❌ Backup failed for table: $table\n";
        $backupStats['errors']++;
    }
}

// ======================= Retention Policy ======================= //

/**
 * Retention Policy:
 * 
 * 1. For backups within the past RETENTION_HOURLY_DAYS, keep all hourly backups.
 * 2. For backups older than RETENTION_HOURLY_DAYS, keep only one backup per day per table.
 * 
 * Optional:
 * - Define an overall retention period (e.g., 365 days) to delete backups beyond that.
 */

// Calculate timestamps for retention
$now = time();
$twoWeeksAgo = strtotime("-" . RETENTION_HOURLY_DAYS . " days");
$overallRetentionLimit = strtotime("-" . RETENTION_DAILY_DAYS . " days");

// Retrieve all SQL backup files in the backup directory
$backupFiles = glob($backupDir . "*.sql");

if ($backupFiles === false) {
    die("Failed to read backup directory: $backupDir\n");
}

// Organize backups by table
$backupsByTable = [];

foreach ($backupFiles as $file) {
    // Extract the filename
    $filename = basename($file);

    // Use regex to extract table name and datetime
    if (preg_match('/^(.*?)_backup_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.sql$/', $filename, $matches)) {
        $table = $matches[1];
        $date = $matches[2]; // Format: Y-m-d
        $time = $matches[3]; // Format: H-i-s
        $timestamp = strtotime("$date $time");

        if (!isset($backupsByTable[$table])) {
            $backupsByTable[$table] = [];
        }

        $backupsByTable[$table][] = [
            'file' => $file,
            'timestamp' => $timestamp,
            'date' => $date,
            'time' => $time
        ];
    }
}

// Iterate through each table's backups to apply retention policy
foreach ($backupsByTable as $table => $backups) {
    // Sort backups by timestamp descending (newest first)
    usort($backups, function($a, $b) {
        return $b['timestamp'] - $a['timestamp'];
    });

    $keepFiles = [];
    $datesKept = [];

    foreach ($backups as $backup) {
        $file = $backup['file'];
        //get timestamp from $file 
        $timestamp = filemtime($file);

        // $timestamp = $backup['timestamp'];
        $backupDate = $backup['date']; // Y-m-d
        if ($timestamp >= $twoWeeksAgo) {
            // Within the past two weeks: Keep all backups
            $keepFiles[] = $file;
        } else {
            // Older than two weeks: Keep one backup per day
            if (!isset($datesKept[$backupDate])) {
                $keepFiles[] = $file;
                $datesKept[$backupDate] = true;
            }
        }

        // Optional: Implement an overall retention limit
        if (defined('RETENTION_DAILY_DAYS') && $overallRetentionLimit && $timestamp < $overallRetentionLimit) {
            // Mark for deletion beyond the overall retention period
            if (!in_array($file, $keepFiles)) {
                if (unlink($file)) {
                    echo "🗑️ Deleted backup beyond overall retention: $file\n";
                } else {
                    echo "⚠️ Failed to delete backup beyond overall retention: $file\n";
                }
            }
        }
    }
    print_r($keepFiles);

    

    // Now, delete backups not in keepFiles and within the daily retention window
    foreach ($backups as $backup) {
        $file = $backup['file'];

        // Skip if the file is marked to keep
        if (in_array($file, $keepFiles)) {
            continue;
        }

        // Determine if the file is beyond two weeks
        if ($backup['timestamp'] < $twoWeeksAgo) {
            // Attempt to delete the file
            if (unlink($file)) {
                echo "🗑️ Deleted old backup file: $file\n";
                $backupStats['backups_deleted']++;
            } else {
                echo "⚠️ Failed to delete old backup file: $file\n";
                $backupStats['errors']++;
            }
        }
    }
}

    // Send success heartbeat with stats
    $clientMonitor->sendHeartbeatWithStats(
        'success',
        "MySQL backup completed. Hourly: {$backupStats['hourly_backups']}, Daily: {$backupStats['daily_backups']}, Skipped: {$backupStats['tables_skipped']}, Deleted old: {$backupStats['backups_deleted']}",
        $backupStats
    );

} catch (Exception $e) {
    // Send error heartbeat
    if (isset($clientMonitor)) {
        $clientMonitor->sendHeartbeatWithStats(
            'error',
            'MySQL backup failed: ' . $e->getMessage(),
            array_merge($backupStats, ['error_type' => get_class($e)])
        );
    }
    throw $e;
}

?>
