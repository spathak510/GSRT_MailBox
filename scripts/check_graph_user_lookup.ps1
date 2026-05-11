$ErrorActionPreference = 'Stop'

try {
    $kv = @{}
    foreach ($line in Get-Content .env) {
        if ($line -match '^\s*#' -or $line -notmatch '=') {
            continue
        }
        $parts = $line.Split('=', 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim()
        if ($key) {
            $kv[$key] = $value
        }
    }

    $required = @('GRAPH_TENANT_ID', 'GRAPH_CLIENT_ID', 'GRAPH_CLIENT_SECRET', 'GRAPH_MAILBOX_USER')
    $missing = $required | Where-Object { -not $kv.ContainsKey($_) -or [string]::IsNullOrWhiteSpace($kv[$_]) }
    if ($missing.Count -gt 0) {
        throw ("Missing env vars: " + ($missing -join ', '))
    }

    $tokenUri = "https://login.microsoftonline.com/$($kv['GRAPH_TENANT_ID'])/oauth2/v2.0/token"
    $tokenResp = Invoke-RestMethod -Method Post -Uri $tokenUri -ContentType 'application/x-www-form-urlencoded' -Body @{
        client_id = $kv['GRAPH_CLIENT_ID']
        client_secret = $kv['GRAPH_CLIENT_SECRET']
        scope = 'https://graph.microsoft.com/.default'
        grant_type = 'client_credentials'
    }

    if (-not $tokenResp.access_token) {
        throw 'No access_token in token response'
    }

    Write-Output 'TOKEN_OK'

    $mailbox = $kv['GRAPH_MAILBOX_USER']
    $escapedMailbox = $mailbox.Replace("'", "''")
    $filter = [uri]::EscapeDataString("userPrincipalName eq '$escapedMailbox' or mail eq '$escapedMailbox'")
    $userUri = "https://graph.microsoft.com/v1.0/users?`$filter=$filter&`$top=1&`$select=id,userPrincipalName,mail"
    $userResp = Invoke-RestMethod -Method Get -Uri $userUri -Headers @{
        Authorization = "Bearer $($tokenResp.access_token)"
        Accept = 'application/json'
    }

    if (-not $userResp.value -or $userResp.value.Count -eq 0) {
        throw "No user found in tenant for GRAPH_MAILBOX_USER=$mailbox"
    }
    $user = $userResp.value[0]

    Write-Output 'USER_LOOKUP_OK'
    Write-Output ("USER_ID=" + $user.id)
    Write-Output ("USER_UPN=" + $user.userPrincipalName)
    Write-Output ("USER_MAIL=" + $user.mail)
}
catch {
    Write-Output 'USER_LOOKUP_FAILED'
    if ($_.Exception.Response -and $_.Exception.Response.GetResponseStream()) {
        try {
            $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
            $body = $reader.ReadToEnd()
            if (-not [string]::IsNullOrWhiteSpace($body)) {
                Write-Output $body
            }
        }
        catch {
            # Ignore stream-read failures and fall through to existing message output.
        }
    }
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
        Write-Output $_.ErrorDetails.Message
    }
    else {
        Write-Output $_.Exception.Message
    }
    exit 1
}
