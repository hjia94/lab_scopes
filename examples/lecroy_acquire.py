from lab_scopes.lecroy import LeCroyScope


with LeCroyScope("192.168.7.91", verbose=True) as scope:
    data, header = scope.acquire("C1")
    print(scope.idn_string)
    print(len(data), len(header))
