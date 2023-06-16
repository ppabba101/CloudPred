import copy
import cloudpred
import numpy as np
import sklearn.mixture
import torch
import math
import logging


def train(Xtrain, Xvalid, centers=2, regression=False):
    X = np.concatenate([x for (x, *_) in Xtrain]) # Xtrain is a list of tuples (x, y, *_) (patient samples) where x is the input and y is the output

    gm = [] # List of all the samples from all the patients (so list of list of cells)
    for X, *_ in Xtrain: 
        gm.append(X) 

    gm = np.concatenate(gm) # Long list of all the samples from all the patients
    model = sklearn.mixture.GaussianMixture(centers, "diag") # Gaussian mixture model with 2 components
    gm = model.fit(gm) # Fit the model to the data

    component = [Gaussian(torch.Tensor(gm.means_[i, :]), 
                          torch.Tensor(1. / gm.covariances_[i, :])) for i in range(centers)] # Initialize the Gaussian components w/ means and inverse covariance matrices
    mixture = Mixture(component, gm.weights_) # Initialize the mixture model with the Gaussian components and weights
    classifier = DensityClassifier(mixture, centers, 2)

    X = torch.cat([mixture(torch.Tensor(X)).unsqueeze_(0).detach() for (X, y, *_) in Xtrain])
    if regression:
        y = torch.FloatTensor([y for (X, y, *_) in Xtrain])
    else:
        y = torch.LongTensor([y for (X, y, *_) in Xtrain])

    Xv = torch.cat([mixture(torch.Tensor(X)).unsqueeze_(0).detach() for (X, y, *_) in Xvalid])
    if regression:
        yv = torch.FloatTensor([y for (X, y, *_) in Xvalid])
    else:
        yv = torch.LongTensor([y for (X, y, *_) in Xvalid])

    logger = logging.getLogger(__name__)
    # Set weights of classifier for multiple attempts
    for lr in [1e2, 1e1, 1e0, 1e-1, 1e-2, 1e-3]:
        # Repeated attempts to train the classifier via gradient descent to find the best weights
        optimizer = torch.optim.SGD(classifier.pl.parameters(), lr=lr, momentum=0.9)
        if regression:
            criterion = torch.nn.modules.MSELoss()
        else:
            criterion = torch.nn.modules.CrossEntropyLoss()
        best_loss = float("inf")
        best_model = copy.deepcopy(classifier.pl.state_dict())
        logger.debug("Learning rate: " + str(lr))
        # 1000 epochs of gradient descent
        for i in range(1000):
            z = classifier.pl(X)
            if regression:
                z = z[:, 1]
            loss = criterion(z, y)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            zv = classifier.pl(Xv)
            if regression:
                zv = zv[:, 1]
            loss = criterion(zv, yv)
            if i % 100 == 0:
                logger.debug(str(loss.detach().numpy()))
            if loss < best_loss:
                best_loss = loss
                best_model = copy.deepcopy(classifier.pl.state_dict())
        classifier.pl.load_state_dict(best_model)

    reg = None
    return cloudpred.utils.train_classifier(Xtrain, Xvalid, [], classifier, regularize=reg,
                                            iterations=1000, eta=1e-4, stochastic=True,
                                            regression=regression)


def eval(model, Xtest, regression=False):
    reg = None
    model, res = cloudpred.utils.train_classifier([], Xtest, [], model, regularize=reg,
                                                  iterations=1, eta=0, stochastic=True,
                                                  regression=regression)
    return res


class Gaussian(torch.nn.Module): # Gaussian model that returns probability of x under the model
    def __init__(self, mu, invvar):
        super(Gaussian, self).__init__()
        self.mu = torch.nn.parameter.Parameter(mu) # mu is a 2-long vector of means for both clusters
        self.invvar = torch.nn.parameter.Parameter(invvar) # invvar is a 2-long vector of inverse variances for both clusters

    def forward(self, x):
        invvar = torch.abs(self.invvar).clamp(1e-5) # Clamp the inverse variance to be at least 1e-5 to avoid division by 0 (also why abs?)
        return -0.5 * (math.log(2 * math.pi) - torch.sum(torch.log(invvar))
                       + torch.sum((self.mu - x) ** 2 * invvar, dim=1)) # Returns log probability of x under the Gaussian model


class Mixture(torch.nn.Module): # Mixture model that returns probability of x under the model
    def __init__(self, component, weights):
        super(Mixture, self).__init__()
        self.component = torch.nn.ModuleList(component)
        self.weights = torch.nn.parameter.Parameter(torch.Tensor(weights).unsqueeze_(1))

    def forward(self, x):
        logp = torch.cat([c(x).unsqueeze(0) for c in self.component]) # Concatenate the log probabilities of x under each Gaussian model component
        shift, _ = torch.max(logp, 0)
        p = torch.exp(logp - shift) * self.weights
        return torch.mean(p / torch.sum(p, 0), 1) # Returns the relative abundances of the two clusters in X


class DensityClassifier(torch.nn.Module):
    def __init__(self, mixture, centers, states=2): # States refers to classes?
        super(DensityClassifier, self).__init__()
        self.mixture = mixture
        self.pl = PolynomialLayer(centers, states)

    def forward(self, x):
        self.d = self.mixture(x).unsqueeze_(0) # Calculate relative abundances of the two clusters in X, which is caluclated using the Gaussian mixture model
        return self.pl(self.d) 


class PolynomialLayer(torch.nn.Module):
    def __init__(self, centers, states=2):
        super(PolynomialLayer, self).__init__()
        self.polynomial = torch.nn.ModuleList([Polynomial(centers) for _ in range(states - 1)])

    def forward(self, x):
        return torch.cat([torch.zeros(x.shape[0], 1)]
                         + [p(x).unsqueeze_(1) for p in self.polynomial], dim=1)


class Polynomial(torch.nn.Module):
    def __init__(self, centers=1, degree=2):
        super(Polynomial, self).__init__()
        self.centers = centers
        self.degree = degree
        self.a = torch.nn.parameter.Parameter(torch.zeros(degree, centers))
        self.c = torch.nn.parameter.Parameter(torch.zeros(1))

    def forward(self, x):
        return torch.sum(sum([self.a[i, :] * (x ** (i + 1)) for i in range(self.degree)]), dim=1) + self.c

    def linear_reg(self, xy):
        x = np.concatenate(list(map(lambda x: x[0].reshape(1, -1), xy)))
        y = np.array(list(map(lambda x: x[1], xy)))
        y = 2 * y - 1
        x = np.concatenate([x ** (i + 1) for i in range(self.degree)] + [np.ones((x.shape[0], 1))], axis=1)
        w = np.dot(np.linalg.pinv(x), y)
        self.a.data = torch.Tensor(w[:-1].reshape(self.degree, self.centers))
        self.c.data = torch.Tensor([w[-1]])
